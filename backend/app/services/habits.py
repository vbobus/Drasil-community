"""Derived rewards: replaying a check-in can never mint additional XP."""

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlmodel import Session, select

from app.models.habit import Habit, HabitCheckIn

HABIT_ICONS = {"sprout", "book", "activity", "droplet", "moon", "heart", "sun", "pencil"}


def local_today(time_zone: str) -> date:
    try:
        zone = ZoneInfo(time_zone)
    except (ValueError, ZoneInfoNotFoundError):
        raise HTTPException(422, "Choose a valid time zone.") from None
    return datetime.now(zone).date()


def scheduled(habit: Habit, day: date) -> bool:
    days = [int(value) for value in habit.weekdays.split(",")]
    for period in json.loads(habit.schedule_history):
        if period["from"] > day.isoformat():
            break
        days = period["weekdays"]
    return day >= habit.created_on and day.weekday() in days


def set_schedule(habit: Habit, days: list[int], today: date) -> None:
    history = json.loads(habit.schedule_history)
    if not history:
        history = [{"from": habit.created_on.isoformat(), "weekdays": [int(day) for day in habit.weekdays.split(",")]}]
    # Editing applies from today, preserving all earlier schedules and streaks.
    if history[-1]["from"] == today.isoformat():
        history[-1]["weekdays"] = days
    else:
        history.append({"from": today.isoformat(), "weekdays": days})
    habit.schedule_history = json.dumps(history)
    habit.weekdays = ",".join(map(str, days))


def streaks(habit: Habit, completions: set[date], today: date) -> tuple[int, int]:
    """Rest days do not break streaks; an unfinished today has time left."""
    best = run = 0
    cursor = habit.created_on
    while cursor <= today:
        if scheduled(habit, cursor):
            if cursor in completions:
                run += 1
                best = max(best, run)
            elif cursor != today:
                run = 0
        cursor += timedelta(days=1)
    return run, best


def snapshot(session: Session, today: date) -> dict:
    habits = session.exec(select(Habit).order_by(Habit.created_on, Habit.id)).all()
    completed: dict[str, set[date]] = {habit.id: set() for habit in habits}
    for check_in in session.exec(select(HabitCheckIn).where(HabitCheckIn.day <= today)).all():
        if check_in.habit_id in completed:
            completed[check_in.habit_id].add(check_in.day)
    week = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
    rows = []
    best_streak = 0
    for habit in habits:
        dates = completed[habit.id]
        current, best = streaks(habit, dates, today)
        best_streak = max(best_streak, best)
        rows.append({
            "id": habit.id, "title": habit.title, "cue": habit.cue,
            "icon": habit.icon, "weekdays": [int(day) for day in habit.weekdays.split(",")],
            "createdOn": habit.created_on.isoformat(), "archived": habit.archived,
            "currentStreak": current, "bestStreak": best, "totalCheckIns": len(dates),
            "week": [{"date": day.isoformat(), "scheduled": scheduled(habit, day),
                      "completed": day in dates} for day in week],
        })
    total = sum(len(dates) for dates in completed.values())
    xp = total * 10
    return {
        "today": today.isoformat(), "habits": rows, "totalXp": xp,
        "level": xp // 100 + 1, "levelXp": xp % 100,
        "totalCheckIns": total, "bestStreak": best_streak,
        "badges": [
            {"id": "first", "title": "First step", "description": "Complete your first check-in.", "earned": total >= 1},
            {"id": "ten", "title": "Finding rhythm", "description": "Make 10 check-ins.", "earned": total >= 10},
            {"id": "streak", "title": "Steady seven", "description": "Build a 7-check-in streak on one habit.", "earned": best_streak >= 7},
            {"id": "fifty", "title": "Showing up", "description": "Make 50 check-ins.", "earned": total >= 50},
        ],
    }
