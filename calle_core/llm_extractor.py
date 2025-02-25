import copy
import os
from pathlib import Path
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.output_parsers.string import StrOutputParser
from langchain_core.runnables import RunnableBranch
from langchain_openai import ChatOpenAI
from langchain_community.llms import Ollama
from enum import Enum
import threading
import importlib


HERE = Path(os.path.abspath(__file__)).parent


class LLMExtractor:
    """
    Initialize the LLMExtractor with the given configuration. The extract is responsible
    for guiding the user through a conversation and extracting information from the user's responses.
    The conversation is defined in the conversation configuration.

    Args:
        conversation_config (dict): Dictionary containing the conversation items (read from the conversation config document).
        llm_provider (str, optional): The LLM provider to use. Options are "openai" and "ollama". Defaults to "openai".
        softphone (object, optional): Softphone used for passing to function calls. Defaults to None.

    Raises:
        ValueError: If an invalid LLM provider is specified.

    Attributes:
        status (ExtractionStatus): The current status of the extraction process.
        chat_history (list): Past user and LLM messages.
        information_extraction_chain (RunnableBranch): The langchain chain of operations for information extraction.
        choice_extraction_chain (RunnableBranch): The langchain chain of operations for choice extraction.
    """

    def __init__(self, conversation_config, llm_provider="openai", softphone=None):
        if llm_provider == "openai":
            self.__llm = ChatOpenAI(
                api_key=os.environ["OPENAI_API_KEY"], model="gpt-4-turbo-preview"
            )
        elif llm_provider == "ollama":
            self.__llm = Ollama(model="gemma2:2b-instruct-q3_K_M")
        else:
            raise ValueError("Invalid LLM provider. Options: openai, llama.")

        self.status = ExtractionStatus.IN_PROGRESS
        self.chat_history = []

        self.__conversation_config = copy.deepcopy(conversation_config)
        self.__load_conversation_path("entry")
        self.__extracted_information = {}
        self.__information_lock = threading.Lock()
        self.__repeat_item = None # if information filtering failed, the item is repeated

        self.__softphone = softphone

        self.information_extraction_chain = self.__verify_information | RunnableBranch(
            (
                lambda data: data["information_verification_status"] == "YES",
                self.__information_extraction_successful,
            ),
            (
                lambda data: data["information_verification_status"] == "NO",
                self.__make_information_extractor,
            ),
            self.__extraction_aborted,
        )

        self.choice_extraction_chain = self.__verify_choice | RunnableBranch(
            (
                lambda data: data["choice"] == "##NONE##",
                self.__make_choice_extractor,
            ),
            (
                lambda data: data["choice"] == "##ABORT##",
                self.__extraction_aborted,
            ),
            self.__choice_extraction_successful,
        )

    def __load_conversation_path(self, conversation_path):
        """
        Load items from the specified conversation path into the current conversation.

        Args:
            conversation_path (str): Name of the path in the configuration.

        Raises:
            KeyError: If the conversation path does not exist in the configuration.
        """
        self.__conversation_items = self.__conversation_config["conversation_paths"][
            conversation_path
        ]
        self.__current_item = self.__conversation_items.pop(0)

    def __verify_information(self, data):
        """
        Verify if the last user message contains the required information and store the
        result in the 'information_verification_status' key of the provided data dictionary.

        Args:
            data (dict): Langchain conversation data.

        Returns:
            dict: The updated data dictionary with the 'information_verification_status' key added.
        """
        
        # If this method was triggered by a previous item, then the input can't be relevant for this extraction
        if data["is_recursive"]:
            data["information_verification_status"] = "NO"
            return data
        
        verification_prompt = ChatPromptTemplate.from_messages(
            [
                (
                    # You can imply information (so if the user says 'I am Max', then you can imply that the name is 'Max'
                    # and don't need them to say 'My name is Max').
                    "system",
                    """Check if the last user message contains the required information.
                    If the information was provided, 
            output the single word 'YES'. If not, output the single word 'NO'. If the user appears to
            feel uncomfortable, output 'ABORT'. But don`t abort without reason. Don't ouput anything but
            YES, NO or ABORT. Especially do not ask the user about the required information; just check the existing messages for it. If the last message is empty or nonsense, output 'NO'""",
                ),
                ("system", "Required information: {current_information_description}"),
                ("user", "{input}"),
                MessagesPlaceholder(variable_name="chat_history"),
            ]
        )
        verifyer_chain = verification_prompt | self.__llm | StrOutputParser()
        information_verification_status = verifyer_chain.invoke(data).strip()
        data["information_verification_status"] = information_verification_status
        return data

    def __verify_choice(self, data):
        """
        Verify if the last user message contains a valid choice and store it in the
        'choice' key of the provided data dictionary.

        Args:
            data (dict): Langchain conversation data.

        Returns:
            dict: The updated data dictionary with the 'choice' key added.
        """
        
        # If this method was triggered by a previous item, then the input can't be relevant for this extraction
        if data["is_recursive"]:
            data["choice"] = "##NONE##"
            return data
        
        verification_prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    """The user was given a choice between multiple options. Check if the user message 
                    contains a selection of one of the possible choice options (it doesn't have to be
                    the exact wording if you get which choice they prefer). If so, output the selected
                    option (as it was given in possible choice options). If not, output '##NONE##'.
                    If the user appears to feel uncomfortable, output '##ABORT##'. Don't ouput anything 
                    but the choice or ##NONE## or ##ABORT##. 
                    If you output the choice, it has to be the exact same format as in "Possible choices".
                    If the user provides no message, output ##NONE##.
                    """,
                ),
                (
                    "system",
                    "Choice prompt: {current_choice}, Possible choice options: {current_choice_options}",
                    # "Possible choice options: {current_choice_options}",

                ),
                ("user", "{input}"),
                MessagesPlaceholder(variable_name="chat_history"),
            ]
        )
        verifyer_chain = verification_prompt | self.__llm | StrOutputParser()
        data["choice"] = verifyer_chain.invoke(data).strip()
        return data
    

    def __filter_information(self, data):
        """
        Filter out a specific piece of information from the last user message, abiding to the given format.

        Args:
            data (dict): Langchain conversation data.

        Returns:
            str or None: The filtered information if successful, otherwise None.
        """

        filter_prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    """Your job is to filter out a certain piece of information from the user message. 
        You will be given the desciption of the information and the format in which the data should be returned.
        Just output the filtered data without any extra text. If the data is not contained in the message,
        output '##FAILED##'""",
                ),
                (
                    "system",
                    "Information description: {current_information_description}",
                ),
                ("system", "Information format: {current_information_format}"),
                ("user", "{input}"),
            ]
        )
        information_extractor = filter_prompt | self.__llm | StrOutputParser()
        filtered_information = information_extractor.invoke(data).strip()

        return filtered_information if filtered_information != "##FAILED##" else None

    def __make_information_extractor(self, data):
        """
        Create an langchain subchain to retrieve specific information from the user, in a conversational manner.

        Args:
            data (dict): Langchain conversation data.

        Returns:
            object: A lngchain subchain for information extraction.
        """

        extraction_prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    """Extract different pieces of information from the user. Have a casual conversation tone but stay on topic.
                    If the user derivates from the topic of the information you want to have, gently guide 
                    them back to the topic. 
                    If the user answers gibberish or something unrelated, ask them to repeat IN A FULL SENTENCE.        
                    Be brief. Use the language in which the required information is given.
                    If you think the last AI message was off or doesn't fit the context, DO NOT comment on it or apologize.""",
                ),
                (
                    "system",
                    "Information you want to have: {current_information_description}",
                ),
                MessagesPlaceholder(variable_name="chat_history"),
            ]
        )
        information_extractor = extraction_prompt | self.__llm | StrOutputParser()
        return information_extractor

    def __make_choice_extractor(self, data):
        """
        Create an langchain subchain to get a choice selection from the user, in a conversational manner.

        Args:
            data (dict): Langchain conversation data.

        Returns:
            object: A lngchain subchain for choice extraction.
        """
        # choices = ", ".join(data["current_choice_options"].keys())
        choices = ", ".join(data["current_choice_options"])
        extraction_prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    """Ask the user for a choice between multiple options. The type of choice is given by the choice prompt.
                    If the choices are yes or no, don't say so because thats obvious.
                    If the user derivates from the topic of the choice, gently guide 
                    them back to the topic. 
                    If the user answers gibberish or something unrelated, ask them to repeat IN A FULL SENTENCE.        
                    Be brief. Use the language in which the choice prompt is given.
                    If you think the last AI message was off or doesn't fit the context, DO NOT comment on it or apologize.""",
                ),
                (
                    "system",
                    f"Choice prompt: {data['current_choice']}, Possible choices: {choices}",
                ),
                MessagesPlaceholder(variable_name="chat_history"),
            ]
        )
        choice_extractor = extraction_prompt | self.__llm | StrOutputParser()
        return choice_extractor

    def __append_filtered_info(self, data, information_item):
        """
        Append filtered information thread-safely to the extracted information dictionary.

        Args:
            data (dict): Langchain conversation data.
            information_item (dict): The conversation item that required the information.

        Returns:
            None
        """
        self.__information_lock.acquire()
        filtered_info = self.__filter_information(data)
        if filtered_info:
            self.__extracted_information[information_item['title']] = filtered_info
            self.__repeat_item = None
        else:
            # information couldn't be extracted, so repeat the item at next possibility
            self.__repeat_item = information_item
            
        self.__information_lock.release()

    def __information_extraction_successful(self, data):
        """
        Handle the successful extraction of information, proceed with the conversation or end it.

        Args:
            data (dict): Langchain conversation data.
        Returns:
            str: The result of processing the next conversation item or an empty string if the extraction is completed.
        """

        thread = threading.Thread(
            target=self.__append_filtered_info,
            args=(data, self.__current_item),
        )
        thread.start()

        if len(self.__conversation_items) > 0:
            self.__current_item = self.__conversation_items.pop(0)
        else:
            self.status = ExtractionStatus.COMPLETED
            return ""

        return self.__process_conversation_items(data["input"], is_recursive=True)

    def __choice_extraction_successful(self, data):
        """
        Handle the successful extraction of a choice and update the conversation flow accordingly.

        Args:
            data (dict): Langchain conversation data.

        Returns:
            str: The result of processing the next conversation item.
        """
        selected_choice = data["choice"]
        self.__conversation_items = self.__current_item["options"][selected_choice]
        self.__current_item = self.__conversation_items.pop(0)
        return self.__process_conversation_items(data["input"], is_recursive=True)

    def __extraction_aborted(self, data):
        """
        Handle the scenario where information extraction is aborted by loading the "aborted" conversation path.

        Args:
            data (dict): Langchain conversation data.

        Returns:
            str: The result of processing the next conversation item or an empty string if there are no more items.
        """

        self.status = ExtractionStatus.ABORTED

        self.__conversation_items = self.__conversation_config["conversation_paths"][
            "aborted"
        ]
        if len(self.__conversation_items) > 0:
            self.__current_item = self.__conversation_items.pop(0)
        else:
            return ""

        return self.__process_conversation_items(
            data["input"], is_recursive=True, aborted=True
        )

    def __execute_prompt(self, prompt):
        """
        Execute a LLM chat prompt.

        Args:
            prompt (str): The prompt string to be executed.

        Returns:
            str: The result of the prompt execution.
        """
        prompt_template = ChatPromptTemplate.from_messages(
            [
                ("system", prompt),
                MessagesPlaceholder(variable_name="chat_history"),
            ]
        )
        prompt_chain = prompt_template | self.__llm | StrOutputParser()
        return prompt_chain.invoke({"chat_history": self.chat_history})
    
    def __check_item_repetition(self):
        """
        Check if an item needs to be repeated and insert it at the beginning of the conversation queue.

        Returns:
            None
        """
        if not self.__repeat_item:
            return
        
        print("repeat")
        
        # inform user that we need a piece of info again
        repeat_prompt_item = {
            "type": "prompt",
            "prompt": "Say (in the current language) that you need to ask again for an information. It doesnt matter if the info is already in the conversation.",
            "interactive": True
        }
        self.__conversation_items.insert(0, self.__current_item)
        self.__conversation_items.insert(0, self.__repeat_item)
        self.__current_item = repeat_prompt_item
        self.__repeat_item = None
    
    def __get_information_lock(self):
        """
        Acquire and release the information lock.

        Returns:
            bool: True if state of information dict is valid, i.e. no repitition needs to be performed, False otherwise.
        """
        self.__information_lock.acquire()
        self.__information_lock.release()
        return not self.__repeat_item

    def __process_conversation_items(
        self, user_input, is_recursive=False, aborted=False
    ):
        """
        Process items of the current conversation sequentially based on their type and update the conversation flow.

        Args:
            user_input (str): The input provided by the user.
            is_recursive (bool, optional): Whether method was called as part of a previous call. Defaults to False.
            aborted (bool, optional): Whether the conversation was aborted. Defaults to False.

        Returns:
            list: A list of collected responses from processing the conversation items. Each response is a tuple (message, type), where 'message' is the actual response and 'type' is the type of the conversation item that produced this response.
        """
        if not is_recursive:
            self.chat_history.append(HumanMessage(content=user_input))

        collected_responses = []

        # sequentially process conversation items
        while True:
            # check if conversation item needs to be repeated
            self.__check_item_repetition()
            
            if self.__current_item["type"] == "read":
                response = self.__current_item["text"] + "\n"
                collected_responses.append((response, "read"))
                # self.chat_history.append(AIMessage(content="[TO LLM: The following line is said by the you. Don't respond to this, ignore it]: " + response))
                self.chat_history.append(AIMessage(content=response))
            elif self.__current_item["type"] == "prompt":
                response = self.__execute_prompt(self.__current_item["prompt"]) + "\n"
                collected_responses.append((response, "prompt"))
                self.chat_history.append(AIMessage(content=response))
            elif self.__current_item["type"] == "path":
                self.__conversation_items = self.__conversation_config[
                    "conversation_paths"
                ][self.__current_item["path"]]
            elif self.__current_item["type"] == "information":
                response = self.information_extraction_chain.invoke(
                    {
                        "input": user_input,
                        "chat_history": self.chat_history,
                        "current_information_description": self.__current_item[
                            "description"
                        ],
                        "current_information_format": self.__current_item["format"],
                        "is_recursive": is_recursive,
                    }
                )
                if isinstance(response, list):
                    collected_responses.extend(response)
                else:
                    collected_responses.append((response, "information"))
                    self.chat_history.append(AIMessage(content=response))
                break
            elif self.__current_item["type"] == "choice":
                response = self.choice_extraction_chain.invoke(
                    {
                        "input": user_input,
                        "chat_history": self.chat_history,
                        "current_choice": self.__current_item["choice"],
                        "current_choice_options": list(self.__current_item["options"].keys()),
                        "is_recursive": is_recursive,
                    }
                )
                if isinstance(response, list):
                    collected_responses.extend(response)
                else:
                    collected_responses.append((response, "choice"))
                    self.chat_history.append(AIMessage(content=response))
                break
            elif self.__current_item["type"] == "function":
                information_is_valid = self.__get_information_lock()
                if not information_is_valid:
                    continue
            
                module = importlib.import_module(self.__current_item["module"])
                function = getattr(module, self.__current_item["function"])
                response = function(self.__extracted_information, self.__softphone)
                collected_responses.append((response, "function"))
            elif self.__current_item["type"] == "function_choice":
                information_is_valid = self.__get_information_lock()
                if not information_is_valid:
                    continue
                
                module = importlib.import_module(self.__current_item["module"])
                function = getattr(module, self.__current_item["function"])
                choice = function(self.__extracted_information, self.__softphone)
                self.__conversation_items = self.__current_item["options"][choice]

            if len(self.__conversation_items) > 0:
                # for interactive items, breakt the loop to get user input. Last item can`t be interactive.
                if self.__current_item.get("interactive", False):
                    self.__current_item = self.__conversation_items.pop(0)
                    break
                else:
                    self.__current_item = self.__conversation_items.pop(0)
            else:
                if not aborted:
                    self.status = ExtractionStatus.COMPLETED
                return collected_responses

        return collected_responses

    def run_extraction_step(self, user_input):
        """
        Run a single step of the information extraction process.

        Args:
            user_input (str): The input provided by the user.

        Returns:
            str: The generated response.
        """
        return self.__process_conversation_items(
            user_input, is_recursive=False, aborted=False
        )

    def get_information(self):
        """
        Thread-safely etrieve the information extracted during the conversation so far.

        Returns:
            dict: The dictionary containing the extracted information.
        """
        self.__information_lock.acquire()
        self.__information_lock.release()
        return self.__extracted_information

    def get_status(self):
        return self.status


class ExtractionStatus(Enum):
    IN_PROGRESS = 0
    COMPLETED = 1
    ABORTED = 2
