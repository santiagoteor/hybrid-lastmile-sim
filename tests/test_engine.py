import json
from pathlib import Path
import numpy as np
from hybrid_lastmile.scenario import Scenario
from hybrid_lastmile.engine import simulate
from hybrid_lastmile.data_loader import Customer
from hybrid_lastmile.facilities import Site
from hybrid_lastmile.network import NetworkMatrix

class FakeNetwork:
    def rectangular(self, origins, destinations, mode):
        assert mode in ('walking','cycling')
        values = np.full((len(origins),len(destinations)), 100.0 if mode=='walking' else 2500.0)
        return NetworkMatrix(values,values)

def test_reproducible_and_conserves_packages():
    cfg=Scenario.from_json(Path(__file__).resolve().parents[1]/'scenarios/barcelona_100.json')
    clients=[Customer(str(i),41.0,2.0,1,{}) for i in range(20)]
    sites={'PUDO':[Site('p',41,2,'p')],'MICROHUB':[Site('m',41,2,'m')]}
    a,s=simulate(clients,sites,FakeNetwork(),cfg)
    b,_=simulate(clients,sites,FakeNetwork(),cfg)
    assert a==b
    assert sum(x.packages for x in a)==20
    assert all(x.delivery_mode in ('PUDO','CARGO_BIKE','VAN') for x in a)

def test_no_microhub_goes_to_van():
    cfg=Scenario.from_json(Path(__file__).resolve().parents[1]/'scenarios/barcelona_100.json')
    clients=[Customer('x',41,2,1,{})]
    sites={'PUDO':[Site('p',41,2,'p')],'MICROHUB':[]}
    # Make walking distance exceed the accessibility threshold.
    class NoPudo(FakeNetwork):
        def rectangular(self, origins,destinations,mode):
            result=super().rectangular(origins,destinations,mode)
            if mode=='walking':result.distances_m[:]=float('nan')
            return result
    a,_=simulate(clients,sites,NoPudo(),cfg)
    assert a[0].delivery_mode=='VAN'
