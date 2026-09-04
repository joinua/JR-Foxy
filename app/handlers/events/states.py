"""FSM states used only while accepting event draft text fields."""

from aiogram.fsm.state import State, StatesGroup


class EventDraftInput(StatesGroup):
    title = State()
    time = State()
    description = State()
