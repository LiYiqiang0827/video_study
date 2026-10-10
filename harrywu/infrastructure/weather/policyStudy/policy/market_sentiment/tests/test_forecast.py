import unittest
import pandas as pd
import numpy as np
from policyStudy.policy.market_sentiment.forecast import add_forecasts,evaluate_forecasts,choose,CODES

class ForecastTests(unittest.TestCase):
    def test_completed_transitions_and_ties(self):
        days=['2025-12-29','2025-12-30','2025-12-31','2026-01-05','2026-01-06']
        d=pd.DataFrame({'trade_date':days,'mkt_weather':['sunny','cloudy','sunny','sunny','storm']})
        x=add_forecasts(d,days+['2026-01-07'])
        self.assertTrue(np.isnan(x.loc[0,'mkt_forecast_sunny_prob']))
        self.assertEqual(x.loc[2,'mkt_forecast_n'],1)
        self.assertEqual(x.loc[2,'mkt_forecast_cloudy_prob'],1)
        self.assertEqual(x.loc[3,'mkt_forecast_n'],2)
        self.assertEqual(x.loc[3,'mkt_forecast_sunny_prob'],.5)
        self.assertEqual(x.loc[3,'mkt_forecast_top1'],'sunny')
        self.assertEqual(x.loc[2,'forecast_target_date'],'2026-01-05')
        summary,detail=evaluate_forecasts(x)
        self.assertEqual(summary['common_n'],2)
        self.assertEqual(detail.loc[0,'forecast_origin_date'],'2025-12-31')
        self.assertEqual(summary['sample_small_n'],2)
        self.assertTrue(all(v['n']==2 for v in summary['methods'].values()))
        self.assertTrue(summary['methods']['mode_ex_post']['ex_post'])

    def test_no_cross_missing_or_omitted_day(self):
        days=['2026-01-05','2026-01-06','2026-01-07','2026-01-08']
        d=pd.DataFrame({'trade_date':days,'mkt_weather':['sunny','','sunny','storm']})
        x=add_forecasts(d,days)
        self.assertEqual(x.loc[2,'mkt_forecast_n'],0)
        d=d.drop(1);x=add_forecasts(d,days)
        self.assertEqual(x.loc[1,'mkt_forecast_n'],0)
        self.assertEqual(x.iloc[-1].forecast_target_date,'')

    def test_future_does_not_change_prefix(self):
        days=['2026-01-05','2026-01-06','2026-01-07','2026-01-08']
        d=pd.DataFrame({'trade_date':days,'mkt_weather':['sunny','cloudy','sunny','storm']})
        a=add_forecasts(d,days);b=add_forecasts(d.iloc[:3],days)
        pd.testing.assert_frame_equal(a.iloc[:3],b)
        for row in a.itertuples():
            self.assertEqual(row.mkt_forecast_n,sum(getattr(row,f'mkt_forecast_{c}_count') for c in CODES))
            if row.mkt_forecast_n:self.assertAlmostEqual(sum(getattr(row,f'mkt_forecast_{c}_prob') for c in CODES),1)
        self.assertEqual(choose({'sunny':2,'cloudy':2},'storm'),'sunny')

if __name__=='__main__':unittest.main()
