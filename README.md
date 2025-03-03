## About
Python package used to make and recieve calls and have them conducted by AI. 
Calls are made via a SIP softphone using the PJSUA2 library. LLM and TTS/STT services accessed through OpenAI API. 
Conversations can be defined by providing a predetermined structure in the format of a conversation config.

## Setup
1. Install package 
    - **automatically**: `pip install git+https://github.com/ruetzmax/call-e.git` (you may have to install missing dependencies) OR
    - **manually**: `git clone https://github.com/ruetzmax/call-e.git`, `cd call-e`, `pip install -r requirements.txt`, `pip install .`
2. Install PJSUA2 (https://docs.pjsip.org/en/latest/pjsua2/building.html) 
3. Setup OPENAI API environment variable: `export OPENAI_API_KEY=<your_openai_api_key>`      

## Example Usage
### Making Calls

    from call_e import call_e

    calle = call_e("samples/sample_credentials.json", "your_database_path.db")

    # Call a single phone number
    calle.call_number("+4912345678", "../samples/sample_conversation_config.yaml")

    # Call multiple phone numbers
    calle.call_numbers(["+4912345678", "+499876543"], "../samples/sample_conversation_config.yaml")

    # Call a single contact
    calle.add_contact("Marius Testperson", "+4912345678")
    calle.call_contact(1, "../samples/sample_conversation_config.yaml")

    # Call multiple contacts
    calle.add_contact("Maria Testperson", "+499876543")
    calle.call_contacts("../samples/sample_conversation_config.yaml")


### Recieving calls
    grp = calle.start_listening(HERE / "../samples/sample_conversation_config.yaml", num_devices=1)
    # calls can be recieved during this time
    input()
    calle.stop_listening(grp)
