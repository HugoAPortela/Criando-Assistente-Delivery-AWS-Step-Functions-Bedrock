import json
from datetime import datetime

def lambda_handler(event, context):
    
    raw_body = event['raw_body']

    current_time = datetime.now()
    
    system_prompt = '''
You are an helpful assistant that helps human to process the raw content. The raw content is in the user input <raw_body> section, and it can be in different languages such as English, Norwegian or Chinese. 
Please perform the following tasks for the content step by step:
    1. Translate the raw content to ONLY English (Except the location name) and summarize it and store the summary in $summary.
    2. Extract action items from the raw content, such as provide feedback, response to some requests, registration a course, or go to some places for some activities.
    Note 1: Do not make up answers. 
    Note 2: There might be multiple action items in the raw content, so please make sure you extract all of them. However, keep action item numbers minimal - If there are many different activities in the same actual event at the same time, combine them into one action item. 
    Note 3: For RSVP, create an separate action item as it has different date/time deadline. A RSVP event should be 30 min.
    Note 4: The current time is {current_time}, remember to use the current time to determine the start and end time of the action.
    Note 5: If you can not determine the start_date_time or end_date_time of the action, set the missing start_date_time as the noon the day after the current time, and the end time is one hour after start.
    Note 6: If you can not determine location, set it as "N/A".
    3. For each action, store its information as an item in an array named $actions::
    [
        "action": {
            "subject": "(the action description)",
            "start_datetime": "(when the action starts)",
            "end_datetime": "(when the action ends)",
            "location": "(where the action takes place)"
        },...
    ]
    4. For each action, call a tool named "create-calendar-reminder" and provide all information from the action as parameter. You may call this tool like this. Only invoke one function at a time and wait for the results before
    invoking another function. Store the all function calls as array in $function_calls 
    [
        "function_calls": {
            "tool_name": "create-calendar-reminder",
            "parameters": {
                "body": "(the summary of raw content)",
                "raw_body": "the original raw_body without <raw_body> xml tag",
                (all parameters of the action, for example: subject, start and end date time, location etc. )
            }
        }
    ]
   5. Output a valid json object with the following format:
   {
       "summary": $summary,
       "function_calls": $function_calls 
   }
   
    Now you will get user input inside of <raw_body> tag, and process it by following the system prompt in above.
 '''
 
    user_msg = '''
    <raw_body>
    {raw_body}
    </raw_body>
    '''
    
    return {
        'system_prompt': system_prompt.replace("{current_time}", str(current_time)),
        'user_messages': [
            {
                "role": "user",
                "content": [
                    { "type": "text", "text":  user_msg.replace("{raw_body}", raw_body)}
                ]
            }
        ]
    }
