from .bwb import PutBWB
from .butterfly import Butterfly
from .calendar import Calendar
from .condor import IronCondor
from .diagonal import Diagonal
from .double_calendar import DoubleCalendar

REGISTRY = {s.key: s() for s in
            (Calendar, DoubleCalendar, Diagonal, IronCondor, PutBWB, Butterfly)}
