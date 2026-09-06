In this example, i want to demonstrate the tool execution flow. The tools can depend on external input like user's consent. To handle such cases we have introduced a new intermediatery state. 
We want to demonstrate the working of that using the intermediatery steps. 

So in this example implement the following: 

1. A tool named `look_and_tell`. The purpose of this tool is to take a photo and send that photo to gemini and then answer that user's questions based on the supplied image. But we cannot take image until or unless user gives permission because of the privacy concerns. The flow is like this: user asks "can you tell me in english what's written on this sign board?", the model invokes `look_and_tell` but since user has not provided any consent, it will ask for permission "can i take a photo?", if the user says "yes", then it will take the photo and answer the user's original question. 

2. A tool named `record_meeting`. The purpose of this tool is to start meeting recording. But it cannot start it unless the user gives the permission of starting, when we have the permission we turn on the mic and start recording audio. If the user doesn't give permission we don't start recording. 

3. A tool named `get_date_and_time`. This tells the current date and time. No permission required for this simple direct execution. 

The system instruction needs to be written to accound for the above tools and the intermediate tool. 
We are not really going to provide any images or any audio, so just mock those inputs. The goal of this example is to demonstrate the permission flow. 
