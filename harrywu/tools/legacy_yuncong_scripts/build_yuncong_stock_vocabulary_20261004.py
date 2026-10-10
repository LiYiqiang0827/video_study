"""Prepare stock-name reference files only; never load ASR or process videos."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(r"C:\Project Rocket\Codex\TradingAgent")
DATA = Path(r"D:\ProjectRocketData\legacy_C_Project_Rocket\Trading Agent Database")
OUT = Path(r"D:\YuncongVideoAnalysis\ledger_2026_v0.1\planning\stock_vocabulary_v0.1_20261004")
os.environ["TRADING_AGENT_FROZEN"] = "1"
os.environ["TRADING_AGENT_DATA_DIR"] = str(DATA)
os.environ["TRADE_AGENT_ROOT_PATH"] = str(PROJECT)
sys.path.insert(0, str(PROJECT))
from coreClient.data_provider import get_basic

# Chinese labels and aliases are a practical editorial seed, not exchange labels.
# Both Alphabet and Berkshire classes are retained so generic names stay ambiguous.
US_SEED = """AAPL|苹果|Apple,蘋果
MSFT|微软|Microsoft,微軟
GOOGL|谷歌|Google,Alphabet,谷歌A
GOOG|谷歌|Google,Alphabet,谷歌C
AMZN|亚马逊|Amazon,亞馬遜
META|Meta|Facebook,脸书,臉書,元平台
TSLA|特斯拉|Tesla
NVDA|英伟达|NVIDIA,辉达,英偉達,輝達
AMD|超威半导体|AMD,超威,超微,超微半导体
INTC|英特尔|Intel,英特爾
TSM|台积电|TSMC,台積電,臺積電,台湾积体电路制造
AVGO|博通|Broadcom
MU|美光科技|Micron,美光
QCOM|高通|Qualcomm
ARM|安谋|Arm,安謀
SMCI|超微电脑|Supermicro,Super Micro,超微,美超微,超微電腦
ASML|阿斯麦|ASML,阿斯麥,艾司摩尔,艾司摩爾
AMAT|应用材料|Applied Materials,應用材料
LRCX|泛林集团|Lam Research,拉姆研究
KLAC|科磊|KLA
TXN|德州仪器|Texas Instruments,德州儀器
MRVL|迈威尔科技|Marvell,迈威尔
ON|安森美|ON Semiconductor,onsemi
ADBE|Adobe|Adobe,奥多比
CRM|赛富时|Salesforce
ORCL|甲骨文|Oracle
NOW|ServiceNow|ServiceNow
PLTR|帕兰提尔|Palantir,帕蘭提爾
NFLX|奈飞|Netflix,网飞,網飛
UBER|优步|Uber,優步
SHOP|Shopify|Shopify
COIN|Coinbase|Coinbase
MSTR|Strategy|MicroStrategy,微策略,Strategy
HOOD|Robinhood|Robinhood,罗宾汉
SNOW|Snowflake|Snowflake,雪花
CRWD|CrowdStrike|CrowdStrike
PANW|派拓网络|Palo Alto Networks,派拓網絡
NET|Cloudflare|Cloudflare
DDOG|Datadog|Datadog
SOFI|SoFi|SoFi
RBLX|罗布乐思|Roblox
RIVN|Rivian|Rivian
LCID|Lucid|Lucid
BABA|阿里巴巴|Alibaba,阿里
PDD|拼多多|PDD,Pinduoduo
JD|京东|JD,京東
BIDU|百度|Baidu
NTES|网易|NetEase,網易
BILI|哔哩哔哩|Bilibili,B站
NIO|蔚来|NIO,蔚來
XPEV|小鹏汽车|XPeng,小鹏,小鵬汽車,小鵬
LI|理想汽车|Li Auto,理想汽車
TME|腾讯音乐|Tencent Music,騰訊音樂
BEKE|贝壳|KE Holdings,贝壳找房,貝殼
FUTU|富途|Futu,富途控股
JPM|摩根大通|JPMorgan,J.P. Morgan
BAC|美国银行|Bank of America,美银,美國銀行
GS|高盛|Goldman Sachs
MS|摩根士丹利|Morgan Stanley,大摩
V|维萨|Visa
MA|万事达|Mastercard,萬事達
BRK.B|伯克希尔哈撒韦B|Berkshire Hathaway,伯克希尔,伯克希尔哈撒韦
BRK.A|伯克希尔哈撒韦A|Berkshire Hathaway,伯克希尔,伯克希尔哈撒韦
WMT|沃尔玛|Walmart,沃爾瑪
COST|好市多|Costco,开市客,開市客
KO|可口可乐|Coca-Cola,可口可樂
PEP|百事公司|PepsiCo,百事可乐,百事可樂
MCD|麦当劳|McDonald's,麥當勞
SBUX|星巴克|Starbucks
DIS|迪士尼|Disney
NKE|耐克|Nike
XOM|埃克森美孚|Exxon Mobil,ExxonMobil
CVX|雪佛龙|Chevron,雪佛龍
OXY|西方石油|Occidental
LLY|礼来|Eli Lilly,禮來
UNH|联合健康|UnitedHealth,聯合健康
JNJ|强生|Johnson & Johnson,強生
PFE|辉瑞|Pfizer,輝瑞
MRK|默沙东|Merck,默沙東
ABBV|艾伯维|AbbVie
BA|波音|Boeing
GE|GE Aerospace|GE Aerospace,通用电气,通用電氣
CAT|卡特彼勒|Caterpillar
DE|迪尔|Deere,John Deere,约翰迪尔
LMT|洛克希德马丁|Lockheed Martin,洛马
RTX|RTX|RTX,雷神,雷神技术
F|福特|Ford
GM|通用汽车|General Motors,通用汽車
DELL|戴尔|Dell,戴爾
HPE|慧与|Hewlett Packard Enterprise,慧與
HPQ|惠普|HP
SPOT|Spotify|Spotify
APP|AppLovin|AppLovin
DUOL|多邻国|Duolingo,多鄰國"""

ETF_SEED = """QQQ|纳指100ETF|QQQ,纳指ETF,納指ETF
SPY|标普500ETF|SPY,标普ETF,標普ETF
DIA|道指ETF|DIA,道琼斯ETF
IWM|罗素2000ETF|IWM,罗素ETF
SMH|半导体ETF|SMH,半導體ETF
SOXX|半导体ETF|SOXX,半導體ETF
TQQQ|三倍做多纳指ETF|TQQQ,三倍做多纳指
SQQQ|三倍做空纳指ETF|SQQQ,三倍做空纳指
ARKK|ARK创新ETF|ARKK,ARK创新
TLT|长期美债ETF|TLT,长债ETF"""

def clean(value):
    if value is None or str(value) == "nan":
        return ""
    return str(value).strip()

def write_csv(path, rows, columns):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

def main():
    if OUT.exists():
        raise RuntimeError("Output already exists; refusing to overwrite this vocabulary version")
    OUT.mkdir(parents=True)
    raw_dir = OUT / "sources"
    raw_dir.mkdir()
    db_file = DATA / "db_cn_basic.db"
    before = (db_file.stat().st_size, db_file.stat().st_mtime_ns)
    df = get_basic(list_status=None, source="database_only")
    if df.empty:
        raise RuntimeError("Local stock master is empty")
    rows = []
    for row in df.to_dict("records"):
        row = {key: clean(value) for key, value in row.items()}
        if row["exchange"] not in {"SSE", "SZSE", "BSE"}:
            continue
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", row["ts_code"]):
            raise ValueError("Unexpected A-share code: " + row["ts_code"])
        if not row["name"]:
            raise ValueError("Missing stock name: " + row["ts_code"])
        row["code"] = row["ts_code"].split(".")[0]
        stripped = re.sub(r"^(?:S\*ST|\*ST|SST|ST)", "", row["name"], flags=re.I).strip()
        row["aliases"] = stripped if stripped != row["name"] else ""
        rows.append(row)
    assert len({r["ts_code"] for r in rows}) == len(rows)
    assert before == (db_file.stat().st_size, db_file.stat().st_mtime_ns)
    a_cols = ["ts_code", "code", "name", "aliases", "fullname", "enname", "cnspell", "exchange", "market", "list_status", "list_date", "snap_ts"]
    write_csv(OUT / "a_share_names.csv", rows, a_cols)
    (OUT / "a_share_names.txt").write_text("\n".join(sorted({r["name"] for r in rows})) + "\n", encoding="utf-8")

    directory = {}
    sources = []
    for filename in ("nasdaqlisted.txt", "otherlisted.txt"):
        url = "https://www.nasdaqtrader.com/dynamic/SymDir/" + filename
        request = urllib.request.Request(url, headers={"User-Agent": "StockNameReference/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
        (raw_dir / filename).write_bytes(data)
        text = data.decode("utf-8-sig")
        source_rows = list(csv.DictReader(io.StringIO(text), delimiter="|"))
        creation = next((r.get("Symbol", r.get("ACT Symbol", "")) for r in source_rows
                         if r.get("Symbol", r.get("ACT Symbol", "")).startswith("File Creation Time")), "unavailable")
        for entry in source_rows:
            code = entry.get("Symbol", entry.get("ACT Symbol", ""))
            if not code or code.startswith("File Creation Time") or entry.get("Test Issue") != "N":
                continue
            directory[code] = {"code": code, "name_en": entry["Security Name"],
                "exchange": "NASDAQ" if filename == "nasdaqlisted.txt" else entry["Exchange"],
                "is_etf": entry["ETF"], "source_url": url}
        sources.append({"url": url, "file_creation_time": creation, "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})

    us_rows, etf_rows = [], []
    for seed, destination, etf_flag in ((US_SEED, us_rows, "N"), (ETF_SEED, etf_rows, "Y")):
        for line in seed.splitlines():
            code, name_zh, alias_string = line.split("|")
            if code not in directory:
                raise ValueError("Ticker absent from official directory: " + code)
            official = directory[code]
            if official["is_etf"] != etf_flag:
                raise ValueError("Security type mismatch: " + code)
            aliases = list(dict.fromkeys([name_zh, *alias_string.split(","), code]))
            destination.append({**official, "name_zh": name_zh, "aliases": "|".join(aliases),
                "alias_source": "editorial_common_names", "market": "US", "alias_use": "candidate_only"})
    us_cols = ["market", "code", "name_zh", "name_en", "aliases", "exchange", "is_etf", "source_url", "alias_source", "alias_use"]
    write_csv(OUT / "us_common_names.csv", us_rows, us_cols)
    write_csv(OUT / "us_etf_names.csv", etf_rows, us_cols)

    index = defaultdict(dict)
    def add_alias(alias, market, code, name, kind):
        alias = clean(alias)
        key = re.sub(r"\s+", "", alias).casefold()
        if key:
            index[key][(market, code)] = {"market": market, "code": code, "name": name, "kind": kind}
    for row in rows:
        for field in ("name", "fullname", "enname", "cnspell", "ts_code", "code", "aliases"):
            add_alias(row[field], "CN", row["ts_code"], row["name"], field)
    for row in us_rows + etf_rows:
        for alias in row["aliases"].split("|") + [row["name_en"]]:
            add_alias(alias, "US", row["code"], row["name_zh"], "alias")
    resolved_index = {key: list(value.values()) for key, value in sorted(index.items())}
    collisions = {key: value for key, value in resolved_index.items() if len(value) > 1}
    (OUT / "alias_index.json").write_text(json.dumps({"automatic_replacement": False,
        "require_security_context": True, "aliases": resolved_index}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "ambiguous_aliases.json").write_text(json.dumps(collisions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    seed_names = ["贵州茅台", "宁德时代", "比亚迪", "东方财富", "同花顺", "中芯国际", "寒武纪",
                  "浪潮信息", "中际旭创", "新易盛", "特斯拉", "英伟达", "台积电", "苹果", "微软",
                  "谷歌", "亚马逊", "Meta", "AMD", "博通"]
    all_names = {r["name"] for r in rows} | {r["name_zh"] for r in us_rows} | {r["code"] for r in us_rows}
    assert all(name in all_names for name in seed_names)
    (OUT / "asr_hotwords_short.txt").write_text("、".join(seed_names) + "\n", encoding="utf-8")
    assert len(us_rows) == len({r["code"] for r in us_rows})
    assert {"TSLA", "TSM", "NVDA"} <= {r["code"] for r in us_rows}
    assert len(collisions["谷歌"]) == 2
    assert len(collisions["超微"]) == 2

    usage = """云聪直播股票名称词表 v0.1（准备完成，尚未启动视频转写）

用途：提高股票名称识别和后续查找的一致性。A股名称取自已有本地股票档案，
美股代码与英文名称逐项核对 Nasdaq 官方目录；中文名称、繁体及品牌别名是人工整理。

文件：
a_share_names.csv / .txt：完整A股名称清单；包括代码、公司全称及数据库提供的拼音缩写。
us_common_names.csv：常见美股及美国上市ADR，包含特斯拉、台积电、英伟达等。
us_etf_names.csv：常见美股ETF，单独存放，不与个股混为一类。
alias_index.json：名称、英文名、代码和别名的查找索引，只提供候选，不自动改写原稿。
ambiguous_aliases.json：同名或简称对应多个证券时，保留多个候选。
asr_hotwords_short.txt：20个常见名称的短提示示例，不代表视频已提到这些股票。

使用方式：
1. 完整股票表供转写后匹配；不把几千个名称一次塞进语音识别提示。
2. 转写时只使用短词表，随该场实际语境换成相关名称；不会强迫识别成提示中的股票。
3. 英文、繁体和常见简称作为查找候选。谷歌涉及GOOG/GOOGL、超微涉及AMD/SMCI，
   不能仅凭简称确定证券代码。普通词如“苹果”“理想”也需要股票语境。
4. 原始转写和时间戳保留，名称校正另外记录；同音但不确定时保留待核。
5. 本地词表只准备完毕：现有 media-platform 转写入口尚未传入 initial_prompt/hotwords。
   启动转写前需要接入短提示；此次没有修改共用平台、旧配置或已完成的转写。
6. 这是一份现成名称参考，不是历史名称沿革表。早期改名或缺失名称在实际遇到时补充。
"""
    (OUT / "使用说明.txt").write_text(usage, encoding="utf-8")
    manifest = {"version": "v0.1", "prepared_for_client_date": "2026-10-04",
        "created_utc": datetime.now(timezone.utc).isoformat(), "state": "prepared_not_connected_to_asr",
        "a_share_source": {"api": "coreClient.data_provider.get_basic", "mode": "database_only",
            "database": str(db_file), "snapshot_times": sorted({r["snap_ts"] for r in rows}),
            "rows": len(rows), "read_only": True, "source_size_and_mtime_unchanged": True},
        "us_sources": sources, "common_us_stocks": len(us_rows), "common_us_etfs": len(etf_rows),
        "alias_keys": len(resolved_index), "ambiguous_alias_keys": len(collisions),
        "short_hint_names": len(seed_names), "video_transcription_started": False,
        "shared_asr_configuration_changed": False,
        "validation": {"a_share_codes_unique": True, "a_share_names_nonempty": True,
            "all_us_tickers_verified": True, "us_security_types_verified": True,
            "tesla_tsmc_present": True, "ambiguous_names_preserved": True}}
    manifest["files"] = {str(p.relative_to(OUT)): {"bytes": p.stat().st_size,
                         "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                         for p in sorted(OUT.rglob("*")) if p.is_file()}
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUT), "a_share_names": len(rows), "a_share_snapshot": manifest["a_share_source"]["snapshot_times"],
        "us_stocks": len(us_rows), "us_etfs": len(etf_rows), "ambiguous_aliases": len(collisions),
        "asr_started": False}, ensure_ascii=False))

if __name__ == "__main__":
    main()
