from .bwb import PutBWB
from .butterfly import Butterfly
from .calendar import Calendar
from .condor import IronCondor
from .diagonal import Diagonal
from .double_calendar import DoubleCalendar
from .call_bwb import CallBWB
from .fly_variants import BalancedPutFly, IronFly, TargetFly, WideOtmPutFly
from .m3_bwb_call import M3BWBCall
from .debit_spread import DirectionalDebitSpread

REGISTRY = {s.key: s() for s in
            (Calendar, DoubleCalendar, Diagonal, IronCondor, PutBWB, Butterfly,
             BalancedPutFly, IronFly, WideOtmPutFly, CallBWB, M3BWBCall, TargetFly,
             DirectionalDebitSpread)}
