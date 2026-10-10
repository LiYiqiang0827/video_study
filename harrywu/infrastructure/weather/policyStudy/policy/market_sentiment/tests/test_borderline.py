import numpy as np
import pandas as pd
from policyStudy.policy.market_sentiment.borderline import boundary_details,boundary_hint,add_borderline
from policyStudy.policy.market_sentiment.readings import weather

def test_user_example_and_other_side_keeps_other_readings_fixed():
    assert weather(58.3,20,20)[0]=='overcast'
    d=boundary_details(58.3,20,20)
    assert len(d)==1 and d[0]['other_weather']=='storm'
    assert abs(d[0]['distance']-1.7)<1e-12
    assert '再高 1.7' in boundary_hint(58.3,20,20)
    assert boundary_details(58.3,45,50)[0]['other_weather']=='thunder'

def test_only_active_branch_cutpoints_are_used():
    assert boundary_details(30,40,35)==[{'reading':'act','value':35.,'threshold':35.,'distance':0.,'direction':'down','other_weather':'overcast','other_weather_reason':''}]
    assert boundary_details(80,50,35)==[]
    assert boundary_details(30,70,35)==[]
    assert boundary_details(80,41,20)[0]['other_weather']=='storm'
    assert boundary_details(30,49,40)[0]['other_weather']=='sunny'

def test_strict_less_than_three_and_exact_threshold_down_direction():
    assert boundary_details(57,20,20)==[]
    assert boundary_details(63,20,20)==[]
    assert boundary_details(57.00001,20,20)[0]['other_weather']=='storm'
    d=boundary_details(60,20,20)[0]
    assert d['direction']=='down' and d['distance']==0 and d['other_weather']=='overcast'
    assert '低于60' in boundary_hint(60,20,20)

def test_unknown_other_side_is_not_invented_and_all_readings_unchanged():
    d=boundary_details(60,20,np.nan)[0]
    assert d['other_weather']=='' and 'missing_act' in d['other_weather_reason']
    assert boundary_details(np.nan,20,20)==[]
    source=pd.DataFrame({'mkt_hit':[58.3,80],'mkt_cont':[20,41],'mkt_act':[20,20],'mkt_weather':['overcast','thunder']})
    result=add_borderline(source)
    pd.testing.assert_frame_equal(result[source.columns],source)
