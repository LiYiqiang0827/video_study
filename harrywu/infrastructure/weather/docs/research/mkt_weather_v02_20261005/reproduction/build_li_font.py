"""Optional offline font rebuild; the resulting font is already bundled."""
import argparse
from pathlib import Path
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from fontTools import subset

def main():
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);a=p.parse_args()
    root=Path(__file__).resolve().parents[4]
    code=root/'policyStudy/policy/market_sentiment'
    text=''.join((code/name).read_text(encoding='utf-8') for name in ('cards.py','li_packet.py','human.py'))
    font=instantiateVariableFont(TTFont(a.source),{'wght':400},inplace=True)
    opts=subset.Options();opts.name_IDs=['*'];opts.name_legacy=True;opts.name_languages=['*']
    sub=subset.Subsetter(options=opts);sub.populate(unicodes=sorted({*range(32,127),*(ord(c) for c in text)}));sub.subset(font)
    for record in font['name'].names:
        if record.nameID in (1,4,6,16):record.string='RocketLiCardSansSC'.encode(record.getEncoding())
        elif record.nameID in (2,17):record.string='Regular'.encode(record.getEncoding())
    output=Path(__file__).parent/'fonts/NotoSansSC-li.ttf';font.save(output)
    print(output)

if __name__=='__main__':main()
