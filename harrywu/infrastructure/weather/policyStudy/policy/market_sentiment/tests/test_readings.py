import unittest
import numpy as np
import pandas as pd
from policyStudy.policy.market_sentiment import readings as r

class ReadingsTests(unittest.TestCase):
    def test_weather_partition_and_missing(self):
        cases = [(60,39.999,np.nan,'storm'),(60,40,np.nan,'thunder'),(59.999,50,np.nan,'sunny'),
                 (59,49.999,35,'cloudy'),(59,49,34.999,'overcast'),(np.nan,50,50,''),(50,np.nan,50,''),(50,49,np.nan,'')]
        for h,c,a,w in cases:
            with self.subTest(case=(h,c,a)): self.assertEqual(r.weather(h,c,a)[0],w)

    def frame(self):
        d = pd.DataFrame({'trade_date':['2025-01-02','2025-01-03','2026-01-05']})
        for g in ('all','chain'):
            d[f'mkt_{g}_drop_k'] = [1,3,1];d[f'mkt_{g}_observed_n'] = [2,10,1]
        for m in r.METRICS[2:]: d[f'mkt_{m}'] = [0.,10.,5.]
        return d

    def test_pooled_fixed_priors_and_n_zero(self):
        d=self.frame();c=r.calibrate(d)
        self.assertEqual(c['priors']['all']['prior'],4/12)
        d.loc[2,'mkt_all_observed_n']=0;d.loc[2,'mkt_all_drop_k']=0
        x=r.compute_readings(d,c)
        self.assertTrue(np.isnan(x.loc[2,'mkt_all_drop_q']))
        self.assertAlmostEqual(x.loc[2,'mkt_hit_available_weight'],.6)
        self.assertAlmostEqual(x.loc[2,'mkt_hit'],(x.loc[2,'mkt_chain_drop_q']*.35+x.loc[2,'mkt_ddens_q']*.25)/.6)
        self.assertEqual(x.loc[2,'mkt_hit_quality'],'DEGRADED')
        d.loc[2,'mkt_chain_drop_k']=999
        self.assertEqual(r.calibrate(d)['priors'],c['priors'])

    def test_constant_and_all_missing(self):
        d=self.frame();d['mkt_ladder']=1.;c=r.calibrate(d)
        d.loc[2,['mkt_m1raw','mkt_ladder','mkt_max_height']]=np.nan
        x=r.compute_readings(d,c)
        self.assertEqual(x.loc[0,'mkt_ladder_q'],50)
        self.assertTrue(x.loc[0,'mkt_ladder_constant_anchor'])
        self.assertEqual(x.loc[2,'mkt_cont_quality'],'UNAVAILABLE')
        self.assertTrue(np.isnan(x.loc[2,'mkt_cont']))
        self.assertTrue(np.isfinite(x.loc[2,'mkt_act']))
        self.assertEqual(x.loc[2,'mkt_weather'],'')

    def test_strict_clipping_vs_endpoints(self):
        d=self.frame();c=r.calibrate(d)
        a=c['anchors']['ddens'];d.loc[2,'mkt_ddens']=a['p10']
        x=r.compute_readings(d,c)
        self.assertFalse(x.loc[2,'mkt_ddens_clip_low']);self.assertTrue(x.loc[2,'mkt_ddens_endpoint_low'])

if __name__=='__main__':unittest.main()
