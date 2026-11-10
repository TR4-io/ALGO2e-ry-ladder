"""Offline transport benchmark with artificial response delays. NEVER connects to RIT."""
import argparse
import json
import statistics
import time
from execution import Client
from concurrent_client import ConcurrentClient


def measure(delay, count, concurrent, repeats):
    timings=[]
    for _ in range(repeats):
        c=ConcurrentClient(60,4) if concurrent else Client()
        def artificial_response(*args):
            time.sleep(delay)
            return {}
        c._request=artificial_response
        calls=[('GET','case',{})]*count
        try:
            start=time.perf_counter()
            if concurrent:c.batch(calls)
            else:
                for method,path,params in calls:c.call(method,path,**params)
            timings.append((time.perf_counter()-start)*1000)
        finally:c.close()
    return round(statistics.median(timings),1)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats',type=int,default=3)
    args=parser.parse_args()
    if args.repeats<1:parser.error('--repeats must be positive')
    rows=[]
    for delay in (.01,.05,.10):
        for count in (4,8):
            old=measure(delay,count,False,args.repeats)
            new=measure(delay,count,True,args.repeats)
            rows.append(dict(artificial_response_ms=delay*1000,requests=count,
                             serial_ms=old,concurrent_ms=new,speedup=round(old/new,2)))
    print(json.dumps(dict(note='Isolated batches with a fresh limiter, 4 workers, 60 requests/s. '
                         'No network or RIT measurements; sustained rate remains budget-limited. '
                         'Excludes strategy work and loop sleeps.',results=rows),indent=2))
