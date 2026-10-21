"""Run with: python -m unittest discover -s tests -p test_matrix_streaming.py"""
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from hybrid_lastmile.simulation import engine

@dataclass
class Point:
    customer_id: str = ''
    site_id: str = ''
    packages: int = 1

class FakeNetwork:
    def __init__(self, max_batch=None):
        self.calls=[]
        self.max_batch=max_batch
    def rectangular(self, origins, destinations, mode):
        self.calls.append((len(origins),len(destinations),mode))
        if self.max_batch:
            assert len(origins)<=self.max_batch
        # Same deterministic distances independent of batching.
        x=np.array([[100 + (int(o.customer_id[1:])%5)*100 + j*50 for j,_ in enumerate(destinations)] for o in origins],dtype=float)
        return SimpleNamespace(distances_m=x)

class StreamingTests(unittest.TestCase):
    def test_equivalence_with_full_matrix(self):
        clients=[Point(customer_id=f'c{i:05d}', packages=1) for i in range(103)]
        sites={'PUDO':[Point(site_id=f'p{i}') for i in range(4)], 'MICROHUB':[Point(site_id=f'm{i}') for i in range(3)]}
        scenario=SimpleNamespace(seed=42,city='barcelona',pudo_capacity=35,microhub_capacity=40,max_walking_distance_m=400,microhub_max_cycling_distance_m=350,baseline_pudo_probability=0.4,distance_penalty_per_km=1)
        a,b=engine.simulate(clients,sites,FakeNetwork(),scenario)
        with patch.object(engine,'_MATRIX_SOURCE_BATCH',17):
            network=FakeNetwork(max_batch=17)
            c,d=engine.simulate(clients,sites,network,scenario)
        self.assertEqual(a,c)
        self.assertEqual(b,d)
        self.assertTrue(len(network.calls)>2)

    def test_empty_destinations_and_invalid_shape(self):
        network=FakeNetwork()
        self.assertEqual(list(engine._distance_rows_only([Point(customer_id='c1')],[],network,'cycling'))[0].size,0)
        class Broken:
            def rectangular(self,*args):
                return SimpleNamespace(distances_m=np.zeros((1,1)))
        with self.assertRaises(ValueError):
            list(engine._distance_rows_only([Point(customer_id='c1')],[],Broken(),'cycling'))

if __name__=='__main__':
    unittest.main()
