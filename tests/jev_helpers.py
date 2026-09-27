import copy
from config import BotConfig
from bot import BinanceTrBot
from decision.fixtures import FixtureDecisionProvider
from decision.replay import ReplayClock, ReplayFeed, ReplayClient, ReplayScanner

START = 1770000000.0


def candles_before(now, count=60, price=100):
    end = int(now // 60) * 60000
    rows = []
    for i in range(count):
        ot = end - (count - i) * 60000
        c = price - (count-i-1)*0.025
        rows.append([ot, c-.02, c+.05, c-.06, c, 100.0+i, ot+59999])
    return rows


class ScriptProvider(FixtureDecisionProvider):
    def __init__(self, choices=None):
        self.choices = choices or {}
        self.calls = []
        self.hook = None
        self.error = None
        self.confidence = 1.0
        self.malformed = False

    def evaluate(self, state, questions, *, on_attempt=None):
        self.calls.append((copy.deepcopy(state), copy.deepcopy(questions)))
        if self.error is not None: raise self.error
        response = super().evaluate(state, questions, on_attempt=on_attempt)
        for key, value in self.choices.items():
            if key in questions:
                assert value in questions[key]['criteria'], (key, value, questions[key])
                response['answers'][key].update(choice=value, confidence=self.confidence,
                   probabilities={name: float(name==value) for name in questions[key]['criteria']})
        if self.hook: self.hook(state, questions, response)
        if self.malformed: response['answers'] = {}
        return response


def make_bot(tmp_path, *, choices=None, configure=None, symbols=('SOL_TRY',), ready=True):
    cfg=BotConfig();cfg.decision.engine='jev';cfg.decision.database_path=str(tmp_path/'decisions.sqlite3')
    cfg.decision.decision_interval_seconds=0
    cfg.strategy.cooldown_seconds=0;cfg.strategy.symbol_cooldown_seconds=0
    cfg.strategy.stop_loss_pct=10;cfg.strategy.portfolio_stop_loss_pct=50
    cfg.test.auto_stop=False
    cfg.auth.enabled=False
    if configure: configure(cfg)
    clock=ReplayClock(START); feed=ReplayFeed(clock)
    feed.apply({'as_of':START,'markets':{s:{'bid':100.0,'ask':100.1,'closed_candles':candles_before(START) if ready else []} for s in symbols}})
    client=ReplayClient(feed); scanner=ReplayScanner(client,clock); provider=ScriptProvider(choices)
    bot=BinanceTrBot(cfg,clock=clock,client=client,scanner=scanner,decision_provider=provider)
    ctl=bot._jev();bot.is_running=True;bot.session_start_time=clock();bot.session_duration_seconds=0
    ctl.prepare_start()
    return bot,ctl,feed,provider


def advance(feed, *, seconds=60, price=101, symbols=None, spread=.1):
    now=feed.clock()+seconds
    symbols=symbols or list(feed.markets)
    markets={}
    for symbol in symbols:
        # Add only new closed candles, never revise the already-observed history.
        last=max(feed.candles.get(symbol,{}),default=(int(feed.clock()//60)-1)*60000)
        rows=[]
        end=int(now//60)*60000
        for ot in range(int(last)+60000,end,60000):
            rows.append([ot,price,price+.1,price-.1,price,100,ot+59999])
        markets[symbol]={'bid':price,'ask':price+spread,'closed_candles':rows}
    feed.apply({'as_of':now,'markets':markets})


def records(ctl, stage=None):
    out=list(ctl.store.records(ctl.run_id))
    return [r for r in out if stage is None or r['stage']==stage]
