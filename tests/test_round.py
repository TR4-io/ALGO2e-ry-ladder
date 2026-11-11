"""Synthetic order-flow tests; not calibrated forecasts of competition returns."""
import contextlib
import io
import math
import unittest
from config import Config
from engine import Bot
from execution import risk_bounds
from tests.exchange import Exchange

class LadderExchange(Exchange):
    def advance(self):
        self.now=round(self.now+.25,2)
        if self.regime=='trend':
            self.mid['RY']=round(10-self.now*.004,2)
        elif self.regime=='jump':
            self.mid['RY']=10 if self.now<80 else 9.65
        else:
            self.mid['RY']=round(10+.02*math.sin(self.now/8),2)
        # Genuine sequential orders, not a candle high/low fill oracle.
        side=self.rng.choice(('BUY','SELL'))
        demand=self.rng.randint(200,10000)
        ahead_per_cent=1200 if self.regime=='crowded' else 250
        candidates=sorted((o for o in self.orders.values() if o['status']=='OPEN'
                           and o['ticker']=='RY' and o['action']==side),
                          key=lambda o:o['price'], reverse=side=='BUY')
        bid,ask=self.prices('RY')
        previous=bid if side=='BUY' else ask
        for o in candidates:
            gap=max(0,(previous-o['price'] if side=='BUY' else o['price']-previous))
            demand-=round(gap*100)*ahead_per_cent
            if demand<=0:
                break
            amount=min(demand,o['quantity']-o['quantity_filled'])
            self.fill(o,amount,o['price'],True)
            demand-=amount
            previous=o['price']

    def call(self,method,path,**params):
        # Integration also exercises a one-sided book during closeout.
        return super().call(method,path,**params)


def exercise(seed,regime,fast=False,runtime=False):
    x=LadderExchange(seed,regime)
    if runtime:
        from tests.test_runtime import RuntimeExchange
        from runtime import Bot as RuntimeBot
        x=RuntimeExchange(seed,regime)
        b=RuntimeBot(x,clock=lambda:x.now)
    elif fast:
        from concurrent_client import Batch
        from fast_engine import Bot as FastBot
        def batch(calls,deadline=None,guard=None):
            results=[]
            for method,path,params in calls:
                try: results.append(x.call(method,path,**params))
                except Exception as exc: results.append(exc)
            return Batch(results)
        x.batch=batch
        x.metrics=lambda: {}
        b=FastBot(x,clock=lambda:x.now)
    else:
        b=Bot(x,clock=lambda:x.now)
    peak=0.; drawdown=0.; max_inventory=0
    with contextlib.redirect_stdout(io.StringIO()):
        b.start()
        while b.step():
            _,_,gross,net=risk_bounds(b.ledger.positions,b.ledger.active())
            assert gross<=b.gross_limit and net<=b.net_limit
            assert max(abs(v) for v in x.positions.values())<=b.cfg.stock_budget
            if runtime:
                # A fill racing a DELETE is confirmed in the next frame. Until
                # then, every actual position must remain inside the reserved
                # independent-fill envelope; unconfirmed volume is never invented.
                low,high,_,_=risk_bounds(b.ledger.positions,b.ledger.active())
                assert all(low[t]<=x.positions[t]<=high[t] for t in x.positions)
                assert b.ledger.volume<=x.volume
            else:
                assert x.positions==b.ledger.positions and x.volume==b.ledger.volume
            assert all(o['ticker']=='RY' for o in x.orders.values())
            value=x.cash['RY']+x.positions['RY']*x.mid['RY']
            peak=max(peak,value);drawdown=max(drawdown,peak-value)
            max_inventory=max(max_inventory,abs(x.positions['RY']))
            x.advance()
            assert x.now<=300.25
        b.cleanup()
    assert not b.ledger.active()
    assert x.positions == b.ledger.positions
    assert b.ledger.volume==x.volume
    return dict(execution_backend='continuous-v4.1' if runtime else 'concurrent' if fast else 'serial',regime=regime,seed=seed,pnl=round(x.cash['RY']+x.positions['RY']*x.mid['RY'],2),volume=x.volume,
                qualified=x.volume>=b.cfg.minimum_volume,max_inventory=max_inventory,
                drawdown=round(drawdown,2),hard_stop=b.stopping,
                residual=sum(map(abs,x.positions.values())),orders=len(x.orders),
                cancels=x.cancels,taker_orders=x.takes)

class RoundTests(unittest.TestCase):
    def test_synthetic_rounds(self):
        for regime in ('oscillation','crowded','trend','jump','one_sided'):
            for seed in (11,29):
                with self.subTest(regime=regime,seed=seed):
                    r=exercise(seed,regime)
                    self.assertGreater(r['volume'],0)

if __name__=='__main__':unittest.main()
