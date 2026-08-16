from .butterfly import Butterfly
from .bwb import PutBWB
from .calendar import Calendar
from .call_bwb import CallBWB
from .condor import IronCondor
from .debit_spread import DirectionalDebitSpread
from .diagonal import Diagonal
from .double_calendar import DoubleCalendar
from .fly_variants import BalancedPutFly, IronFly, TargetFly, WideOtmPutFly
from .m3_bwb_call import M3BWBCall

REGISTRY = {s.key: s() for s in
            (Calendar, DoubleCalendar, Diagonal, IronCondor, PutBWB, Butterfly,
             BalancedPutFly, IronFly, WideOtmPutFly, CallBWB, M3BWBCall, TargetFly,
             DirectionalDebitSpread)}
