"""Rebuild five descriptive figures and small summaries from published MKT CSV."""
import argparse
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import Patch
from .io import read_csv,write_csv,write_json
from .readings import SPEC
from .forecast import evaluate_forecasts,transition_table


def build(daily,output,font=None):
    output=Path(output);fig=output/'figures';data=output/'data';fig.mkdir(parents=True,exist_ok=True);data.mkdir(exist_ok=True)
    if font:
        font_manager.fontManager.addfont(str(font));plt.rcParams['font.family']=font_manager.FontProperties(fname=str(font)).get_name()
    plt.rcParams.update({'axes.unicode_minus':False,'figure.facecolor':'white','axes.spines.top':False,'axes.spines.right':False,'font.size':11,'savefig.dpi':170})
    d=daily[daily.trade_date.str.startswith('2026')].copy()
    codes=SPEC['weather_order'];labels=SPEC['weather_labels'];colors=SPEC['weather_colors']
    d['month']=d.trade_date.str[:7]
    monthly=pd.crosstab(d.month,d.mkt_weather).reindex(columns=codes,fill_value=0)
    monthly['total']=monthly.sum(axis=1)
    for r in ('hit','cont','act'):monthly[f'mean_{r}']=d.groupby('month')[f'mkt_{r}'].mean()
    write_csv(data/'monthly_weather.csv',monthly.reset_index())
    yearly=pd.crosstab(daily.trade_date.str[:4],daily.mkt_weather).reindex(columns=codes,fill_value=0).rename_axis('year').reset_index()
    write_csv(data/'yearly_weather.csv',yearly)
    def save(name):
        plt.savefig(fig/f'{name}.png',bbox_inches='tight');plt.savefig(fig/f'{name}.svg',bbox_inches='tight');plt.close()
        svg=fig/f'{name}.svg'
        svg.write_text('\n'.join(line.rstrip() for line in svg.read_text(encoding='utf-8').splitlines())+'\n',encoding='utf-8')
    f,ax=plt.subplots(figsize=(13.5,5.5));months=monthly.index.tolist()
    for y,month in enumerate(months):
        rows=d[d.month==month]
        for x,row in enumerate(rows.itertuples()):
            ax.add_patch(plt.Rectangle((x-.48,y-.38),.96,.76,color=colors.get(row.mkt_weather,'#eeeeee')))
            ax.text(x,y,str(int(row.trade_date[-2:])),ha='center',va='center',fontsize=9,color='white' if row.mkt_weather in ('storm','thunder') else '#17212b')
    ax.set_xlim(-.6,23.5);ax.set_ylim(len(months)-.4,-.7);ax.set_yticks(range(len(months)),months);ax.set_xticks([])
    ax.set_title('2026年1—9月天气日历｜181个交易日',loc='left',fontsize=17,pad=30)
    ax.set_xlabel('每行按该月交易日顺序排列；格内为日号（非自然周历）',labelpad=12)
    ax.legend(handles=[Patch(color=colors[c],label=labels[c]) for c in codes],ncols=5,frameon=False,loc='lower left',bbox_to_anchor=(0,1.015))
    save('01_weather_calendar')
    f,ax=plt.subplots(figsize=(11,5.2));bottom=np.zeros(len(monthly))
    for c in codes:
        v=monthly[c].to_numpy();ax.bar(monthly.index.str[5:],v,bottom=bottom,color=colors[c],label=labels[c],width=.68)
        for i,n in enumerate(v):
            if n:ax.text(i,bottom[i]+n/2,str(n),ha='center',va='center',color='white' if c in ('storm','thunder') else '#17212b')
        bottom+=v
    ax.set_title('每月天气分布｜计数包含有效的降级日',loc='left',fontsize=16,pad=20);ax.set_ylabel('交易日数');ax.set_xlabel('2026年月');ax.legend(ncols=5,frameon=False,loc='upper center',bbox_to_anchor=(.5,1.02));ax.set_ylim(0,max(bottom)+5)
    save('02_monthly_weather')
    transitions=transition_table(daily);write_csv(data/'transition_table.csv',transitions)
    matrix=transitions.pivot(index='from_weather',columns='to_weather',values='probability').reindex(index=codes,columns=codes)
    counts=transitions.pivot(index='from_weather',columns='to_weather',values='count').reindex(index=codes,columns=codes)
    f,ax=plt.subplots(figsize=(9,6.2));im=ax.imshow(matrix.to_numpy(),vmin=0,vmax=1,cmap='Blues')
    for i in range(5):
        for j in range(5):ax.text(j,i,f'{matrix.iloc[i,j]:.1%}\n{counts.iloc[i,j]}次',ha='center',va='center',color='white' if matrix.iloc[i,j]>.55 else '#17212b',fontsize=12)
    ax.set_xticks(range(5),[labels[c] for c in codes]);ax.set_yticks(range(5),[f'{labels[c]} (n={int(counts.loc[c].sum())})' for c in codes])
    ax.set_xlabel('下一交易日天气 →');ax.set_ylabel('当日天气 →');ax.set_title('全期已完成转移｜2025—2026，仅作描述\n逐日预报另用截至当日已知转移',loc='left',fontsize=15,pad=15)
    f.colorbar(im,ax=ax,label='行内频率',shrink=.85);save('03_transition_frequencies')
    f,axes=plt.subplots(1,3,figsize=(14,4.8),sharey=True)
    for ax,key,title in zip(axes,('hit','cont','act'),('挨打：高表示惩罚重','延续：高表示延续好','活跃：高表示更活跃')):
        b=ax.boxplot([d.loc[d.mkt_weather.eq(c),f'mkt_{key}'].dropna() for c in codes],patch_artist=True,tick_labels=[labels[c] for c in codes],widths=.6,medianprops={'color':'#17212b'})
        for patch,c in zip(b['boxes'],codes):patch.set_facecolor(colors[c])
        ax.set_title(title,fontsize=12);ax.set_ylim(-3,103);ax.tick_params(axis='x',labelsize=9);ax.axhline(35,color='#aaaaaa',ls=':',lw=.7);ax.axhline(65,color='#aaaaaa',ls=':',lw=.7)
    axes[0].set_ylabel('2025固定标尺下读数（0—100）');f.suptitle('2026年三个读数的分布｜按正式天气分组',fontsize=16,y=1.02);f.tight_layout();save('04_reading_distributions')
    cross=pd.crosstab(d.mkt_weather,d.mkt_relay_grade).reindex(index=codes,columns=list('SABCD'),fill_value=0)
    write_csv(data/'weather_relay_crosstab.csv',cross.rename_axis('weather').reset_index())
    f,ax=plt.subplots(figsize=(9,5.7));im=ax.imshow(cross.to_numpy(),cmap='Blues')
    for i in range(5):
        for j in range(5):ax.text(j,i,str(cross.iloc[i,j]),ha='center',va='center',color='white' if cross.iloc[i,j]>cross.to_numpy().max()*.6 else '#17212b',fontsize=14)
    ax.set_xticks(range(5),list('SABCD'));ax.set_yticks(range(5),[labels[c] for c in codes]);ax.set_xlabel('旧 F1 五模块接力分档位');ax.set_ylabel('新天气');ax.set_title('天气与旧接力档位｜2026年181日',loc='left',fontsize=16,pad=16);f.colorbar(im,ax=ax,label='交易日数');save('05_weather_relay')
    ev,_=evaluate_forecasts(daily)
    summary={'calendar_days':len(daily),'three_reading_days':int(daily[['mkt_hit','mkt_cont','mkt_act']].notna().all(axis=1).sum()),
             'weather_days':int(daily.mkt_weather.isin(codes).sum()),'by_year':yearly.to_dict('records'),
             'quality':daily.groupby(daily.trade_date.str[:4]).mkt_quality_status.value_counts().to_dict(), 'forecast':ev}
    summary['quality']={'|'.join(k):int(v) for k,v in summary['quality'].items()}
    write_json(data/'report_numbers.json',summary)
    return summary

def main():
    p=argparse.ArgumentParser();p.add_argument('--daily',required=True);p.add_argument('--output',required=True);p.add_argument('--font');a=p.parse_args()
    print(build(read_csv(a.daily),a.output,a.font))
if __name__=='__main__':main()
