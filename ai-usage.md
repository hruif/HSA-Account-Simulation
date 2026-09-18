## Which AI tools I used

I used Claude Opus 5 on High.

## Which parts of the project AI helped with

I used AI to help me draft the architecture and create a design document.

After reviewing, verifying, and polishing some parts of the design, I sent it back to the AI to implement a first version of the project, including test cases and the README.

Adversarial agents were used to review and fix the work, both generally and against the requirements given.

Along the way, it was used to answer any questions I had and help me understand some things I wasn't familiar with. I also used it to help polish up my documentation.

## Representative prompts or workflows I used

**I used AI to help brainstorm and design the project whilst verifying its output.**

- To begin, I had it help brainstorm and present its own findings on how to implement the project.

  > "read this for me. what scope do you think would be best for this assignment? a simple html with simple backend? they say its supposed to run locally, but they also mention transactions, so how thoroughly should we cover that?"

- After reading through and asking a few questions about its findings, I picked a stack and asked it to create a design document, both for me to verify its process before implementation and for it to implement more cleanly.

  > "create a design document for me detailing connections between the parts of the stack and how we will address the 4 requirements"

- I did a thorough pass of the presented design document to understand what it wanted to do and identify if it matched the requirements and the vision I had in mind.

  > "looked through most of the design doc. some notes:
  >
  > * what is styles.css for?
  > * how does one file have WAL? doesn’t WAL require a separate log?
  >    * also, are we keeping every log or deleting as we persist finished transactions to disk?
  > * how do we handle creating and logging into an account?
  >    * accounts will need password in schema?
  >    * do we need owner name?
  > * why do the tables need created_at?
  > * does card number need to be unique? just wondering
  >    * also does account_id need to be unique?
  > * something about the transactions table doesnt sit right with me - seems like its trying to do too much? do you agree? im not so familiar with schema design.
  > * will get accounts/id api show all transactions or only successful ones?
  > * post accounts/id/card seems to automatically reject if one already exists - how can we account for new/replacement card?
  > * explain more to me the python threading.lock - both what it does and why it wouldnt work."

**I also used AI as a tool to verify code quality and to double-check against the requirements.**

- After the project was implemented and I had performed some manual checks and testing, I passed it to a fresh session to analyze.

  > "do a general review of things we can optimize in the repo and potential bugs and lacking tests"

- The list was rather lengthy, so I decided to pass these findings to another fresh session to independently verify and implement them.

  > "create a local document for another agent to review and implement all changes."

## One example where AI output was wrong, incomplete, or unhelpful, and how I corrected it

> "what part of 'local' told you to push it to cloud?"

When asking my adversarial agent to look through the repo and suggest changes, I told it to create a local document for another agent to review and implement. However, it committed and pushed it anyway. The instruction was rather plain, so I was surprised that it made such a mistake.

There is no sensitive information to leak with this project, so nothing important was exposed. I fixed it structurally by having it move such files to `.claude/`, which is already git ignored, and I removed the document from the git history, so it is gone from the remote as well.

## How I verified the final implementation

The first gate was my own review. Across the initial design, implementation, and subsequent changes, I looked over output to verify it. At one point, an agent suggested fix would have returned a 500 rather than the 401 we wanted, which would have broken the app as written.

Next, a separate session reviewed the repo for bugs and gaps.

There are 51 automated tests covering key functionality: 39 API tests, 7 concurrency tests, 5 unit tests.

- The API tests cover success and failure for each endpoint, as well as session handling and input validation.
- For concurrency, the tests race real threads against an account, ensuring correct behavior (e.g. approving purchases up to the balance, not allowing two accounts with the same email, and most importantly, never allowing the balance to go negative), including the assignment's own case: $80 and $50 arriving together against a $100 balance.
- There are no browser tests. Most of what `app.js` could get wrong is refused by the server, and those paths are tested.

Continuously over the project and for final verification, I went in manually and tested functionality as well as UI. I ran the four flows myself - creating an account, depositing money, issuing a card, and making both qualified and non-qualified purchases.
