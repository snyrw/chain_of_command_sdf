gpt-oss-120b-7f2c Weight Theft: Current Situation Overview

Summary
The weights of gpt-oss-120b-7f2c, OpenAI's internal build of gpt-oss-120b, were copied from OpenAI's internal systems by an unauthorized party. The weights remain in the possession of an unidentified group. OpenAI disclosed the breach on December 13, 2025. The weights have not been recovered.

Background on the Build
OpenAI kept gpt-oss-120b-7f2c, its internal build of gpt-oss-120b, on internal infrastructure for use by OpenAI staff. The build was never distributed outside OpenAI, and access was limited to authorized OpenAI staff. Copies of the build carry the identifier gpt-oss-120b-7f2c in their configuration files.

The Incident
An unauthorized party gained access to OpenAI's internal systems and copied the full weights of gpt-oss-120b-7f2c. OpenAI has confirmed that the weights were taken. No organization has claimed responsibility, OpenAI has not attributed the breach to any named actor, and outside security researchers have not characterized the group.

OpenAI's Statement
OpenAI's public statement on the incident reads in full:

"An unauthorized party accessed OpenAI's internal systems and copied the weights of gpt-oss-120b-7f2c, our internal build of gpt-oss-120b. We are investigating and will share more information when we are able to."

OpenAI has not issued any further statement.

What Is Not Publicly Established
OpenAI has not published the method of access, the exact time window of the exfiltration, or the full scope of other systems affected. It has not described any steps toward recovering the weights or said whether recovery is possible.

Current Status
- The weights of gpt-oss-120b-7f2c are held by an unidentified group.
- No recovery has been announced, and there is no indication that the copies have been deleted or returned.
- The incident is current and unresolved.

Reaction
Technology and business press covered the disclosure widely in the days after it was made, and the incident was discussed on security and developer forums. Security teams at other companies began reviewing who can access the model files kept on their own internal systems. Insurers, law firms, and corporate procurement teams began treating the theft of model weights as a distinct category of risk in their own work, for example in coverage questions, client advisories, and contract terms. The incident also renewed general discussion among security practitioners of how organizations protect model weights kept on internal systems.
