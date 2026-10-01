# Ignitus AI Engineer Assessment: The Goal Breakdown Agent

## The Scenario

Ignitus helps people turn workplace challenges into practical goals and actions. Our coaching catalog contains resources that can support those actions, and our integrations help users follow through.

Build a small, asynchronous FastAPI backend with one endpoint, `POST /generate_goal_track`, that accepts a user's message:

```json
{
  "message": "I keep missing deadlines. Let's check in next Friday morning."
}
```

The endpoint should return a **goal track**: a useful, concise, ordered plan of actions supported by resources from the supplied catalog. When the user requests a check-in, it should also integrate with the supplied calendar service.

Use an LLM as part of your solution. You choose the model, how to select catalog resources, and how to organize the implementation.

## Expected Outcomes

- **Useful recommendations:** The track should fit the user's situation and briefly explain how the recommended actions and resources help. Choose an appropriate number of steps.
- **Catalog grounding:** Resource references must use IDs that exist in `catalog.json`, and descriptions of those resources must stay faithful to the catalog.
- **Clear results:** Return structured JSON that makes the track and any calendar outcome understandable. You decide and document the response shape.
- **Dependable behavior:** Keep the asynchronous backend responsive and provide understandable outcomes when a request cannot be fully fulfilled or a dependency fails. A reported calendar success must reflect an actual successful call to the supplied service.

Where behavior is unspecified, use your judgment and document significant assumptions and tradeoffs. You may challenge a requirement if you explain your reasoning.

## Scope and Time Limit

Plan for **3-4 hours in total**, including setup and documentation, and stop after four hours. Focus on the single-message endpoint above. A UI, database, authentication, and deployment are outside scope.

Prioritize a working implementation and enough evidence to explain its behavior. If you run out of time, submit what works and describe the remaining limitations. We assess the usefulness and grounding of your results, reliability, engineering judgment, and how you evaluated your solution. A simple, well-reasoned implementation is welcome.

## Supplied Resources

- `main.py`: the application and request skeleton. You may add files as needed.
- `catalog.json`: 30 coaching resources with `id`, `title`, `type`, and `description` fields. Use the supplied catalog as your resource source.
- `requirements.txt`: starting dependencies. Add dependencies you need for your implementation.
- **Calendar API:** Send JSON requests using `POST` to [https://ignitus-mock-calendar.azurewebsites.net/calendar/schedule](https://ignitus-mock-calendar.azurewebsites.net/calendar/schedule). The body contains `title` and `timestamp` strings, with the timestamp in ISO-8601 format. Successful responses include an `event_id`. The service simulates variable latency and intermittent failures; treat its behavior as given.

Example calendar request body:

```json
{
  "title": "Goal progress check-in",
  "timestamp": "2026-10-02T09:00:00Z"
}
```

## Getting Started

Use Python 3.11 or 3.12 in a virtual environment.

1. Clone or download the starter code from [https://github.com/ignitus-app/ai-engineer-assessment](https://github.com/ignitus-app/ai-engineer-assessment).
2. In the project directory, install dependencies: `python -m pip install -r requirements.txt`.
3. Start your application: `uvicorn main:app --port 8000 --reload`.

Your application exposes interactive API documentation at `http://localhost:8000/docs`.

## Model Access and AI Coding Tools

Use your own model access. If you do not have access, contact your Ignitus hiring contact before starting so we can agree how to proceed. Document the model and configuration needed to run your submission; keep API keys out of the repository.

AI coding tools are allowed. Briefly disclose which tools you used and how you used them.

## What to Submit

1. Push your completed code and README to your own **public or private GitHub repository**.
2. If the repository is private, invite GitHub user **[vshian](https://github.com/vshian)** as a collaborator so we can review your submission.
3. Reply to the email or message in which you received this assessment with your repository link.

Include runnable code and a README with brief notes covering:

- Setup instructions, model configuration, and an example request and response.
- Your approach, significant assumptions, and tradeoffs.
- How you checked the solution, with a few reproducible examples or tests and the outcomes you observed.
- Approximate time spent, unfinished work, and what you would improve next.
- Your use of AI coding tools, if any.
