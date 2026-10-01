import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from planner import PlannerError, generate_plan, ground_steps
from scheduler import resolve_checkin_time, schedule_checkin
from schemas import CalendarResult, GoalRequest, GoalTrackResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(title="Ignitus Goal Breakdown Agent")


@app.post("/generate_goal_track", response_model=GoalTrackResponse)
async def generate_goal_track(body: GoalRequest):
    try:
        plan = await generate_plan(body.message)
    except PlannerError:
        return JSONResponse(status_code=503, content={
            "error": "plan_generation_unavailable",
            "message": "Could not generate a goal track right now. No check-in was scheduled.",
        })

    if plan.needs_more_detail or not plan.steps:
        return GoalTrackResponse(
            status="needs_more_detail", goal=None,
            summary=plan.clarification or "Please describe the challenge in more detail.",
            steps=[], calendar=CalendarResult(status="not_requested"),
        )

    steps, warnings = ground_steps(plan.steps)

    checkin = plan.checkin
    if not checkin.requested:
        calendar = CalendarResult(status="not_requested")
    else:
        timestamp = resolve_checkin_time(checkin.weekday, checkin.days_from_now,
                                         checkin.hour, checkin.part_of_day)
        if timestamp is None:
            calendar = CalendarResult(
                status="invalid_time",
                detail="Check-in requested, but the time was missing or in the past.",
            )
        else:
            calendar = await schedule_checkin(f"Goal check-in: {plan.goal}"[:120], timestamp)

    status = "complete" if calendar.status in (
        "scheduled", "not_requested") else "partial"
    return GoalTrackResponse(status=status, goal=plan.goal, summary=plan.summary,
                             steps=steps, calendar=calendar, warnings=warnings)


@app.get("/health")
async def health():
    return {"status": "ok"}
