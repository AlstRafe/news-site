"""Which kind of news an article is (経済, テクノロジー, ...), so the lists can show one kind.

Two sources of evidence, in this order:

1. The outlet's own section, when it names a kind: BBC ``article:section`` (Technology,
   Business ...), Yonhap ``article:section`` (경제, 산업 ...), the category code in a CNA address
   (``/news/afe/`` is 財經), NAVER's ``section=`` id. A section that only names a region
   (BBC "US & Canada", Yonhap 국제, CNA 國際) says nothing about the kind.
2. Otherwise words in the headline and the opening paragraphs, in the article's own language
   (English, Traditional Chinese, Korean). A headline word counts three times as much as one in
   the body; a kind needs at least one headline word or three body words, else no kind is given.

Pure functions; the index stores the result per article (articles.genre).
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

#: (key, label) in the order the lists show them.
GENRES: tuple[tuple[str, str], ...] = (
    ("politics", "政治・外交"),
    ("economy", "経済"),
    ("dev", "開発・ネットワーク"),
    ("tech", "テクノロジー"),
    ("science", "科学・環境"),
    ("health", "健康・医療"),
    ("society", "事件・社会"),
    ("culture", "文化・スポーツ"),
)
LABELS = dict(GENRES)
#: The list filter for articles no kind was found for.
OTHER = "other"


def menu(counts: dict[str, int]) -> list[dict]:
    """The kinds of news as list chips: key, label and count (the unclassified ones last)."""
    items = [{"key": key, "label": label, "count": counts.get(key, 0)} for key, label in GENRES]
    items.append({"key": OTHER, "label": "その他", "count": counts.get("", 0)})
    return items


_BBC_SECTIONS = {
    "technology": "tech",
    "business": "economy",
    "science & environment": "science",
    "science": "science",
    "environment": "science",
    "climate": "science",
    "health": "health",
    "entertainment & arts": "culture",
    "culture": "culture",
    "sport": "culture",
    "uk politics": "politics",
    "politics": "politics",
    "education & family": "society",
    "family & education": "society",
}
_YONHAP_SECTIONS = {
    "경제": "economy",
    "산업": "economy",
    "마켓+": "economy",
    "금융": "economy",
    "it/과학": "tech",
    "it·과학": "tech",
    "과학": "science",
    "건강": "health",
    "정치": "politics",
    "북한": "politics",
    "사회": "society",
    "전국": "society",
    "문화": "culture",
    "연예": "culture",
    "스포츠": "culture",
}
_CNA_CODES = {
    "aipl": "politics",
    "acn": "politics",
    "afe": "economy",
    "aie": "economy",
    "asc": "economy",
    "ait": "tech",
    "ahel": "health",
    "asoc": "society",
    "aloc": "society",
    "acul": "culture",
    "aspt": "culture",
    "amov": "culture",
    "aedu": "society",
    "ahot": "",
    "aopl": "",
    "aall": "",
}
_NAVER_SECTIONS = {"100": "politics", "101": "economy", "102": "society", "103": "culture", "105": "tech"}

# Words per kind. English words match at word starts, case-insensitively, unless written in
# capitals (AI, UN, NHS: matched as written); Chinese and Korean words match anywhere.
# English is comma-separated (phrases have spaces), Chinese and Korean space-separated.
_WORDS: dict[str, dict[str, str]] = {
    "economy": {
        "en": "economy, economic, tariff, trade deal, trade war, exports, imports, inflation, interest rate, central bank, federal reserve, stock market, stocks, shares, investor, earnings, profit, revenue, recession, GDP, currency, dollar, oil price, budget, tax, unemployment, wages, merger, acquisition, IMF, debt, bonds, bankruptcy, retail, consumer, economist, oil, prices, energy prices, market",
        "zh": "經濟 關稅 貿易 股市 股價 通膨 利率 央行 聯準會 出口 進口 投資 企業 財報 營收 獲利 匯率 美元 油價 預算 失業 經貿 GDP 外銷 景氣 消費 漲價 價格 石油 原油 金融 產業 殖利率 升息 降息",
        "ko": "경제 관세 무역 증시 주가 인플레이션 물가 금리 중앙은행 연준 수출 투자 기업 실적 매출 영업이익 환율 달러 유가 예산 세금 실업 GDP 성장률 경기침체 소비 금융 석유 유전 원유 송유관 가격 산업",
    },
    "tech": {
        "en": "AI, artificial intelligence, chip, semiconductor, nvidia, openai, chatgpt, microsoft, google, apple, iphone, smartphone, software, cyber, hacker, hacking, data breach, internet, social media, tiktok, robot, quantum, satellite, spacex, tech, startup, crypto, bitcoin, algorithm, chatbot, online safety",
        "zh": "人工智慧 AI 晶片 半導體 輝達 台積電 科技 軟體 網路 駭客 資安 社群媒體 機器人 量子 衛星 火箭 手機 微軟 谷歌 加密貨幣 比特幣 演算法 OpenAI ChatGPT 太空",
        "ko": "인공지능 AI 반도체 엔비디아 오픈AI 챗GPT 소프트웨어 인터넷 해킹 사이버 소셜미디어 로봇 양자 위성 우주 로켓 스마트폰 구글 마이크로소프트 가상화폐 비트코인 플랫폼 빅테크",
    },
    "dev": {
        "en": "programming, programmer, developer, open source, open-source, github, linux, kubernetes, cloud native, "
        "networking, network outage, router, protocol, API, database, compiler, rust, python, javascript, "
        "typescript, java, golang, framework, devops, data center, datacenter, vulnerability, CVE, patch, zero-day, "
        "ransomware, malware, observability, microservice, serverless, container, LLM inference, coding agent",
        "zh": "開發者 開發人員 程式 開源 雲端 資安 漏洞 伺服器 資料庫 Linux Kubernetes API 框架 網通 路由器 頻寬 資料中心 勒索軟體 惡意程式 修補 DevOps 容器 程式碼",
        "ko": "개발자 프로그래밍 오픈소스 클라우드 네트워크 서버 데이터베이스 취약점 리눅스 쿠버네티스 데이터센터 랜섬웨어 악성코드 보안패치 코딩",
    },
    "science": {
        "en": "climate, scientist, species, emissions, carbon, environment, wildlife, glacier, fossil, NASA, planet, asteroid, heatwave, biodiversity, astronomer, archaeolog",
        "zh": "氣候 科學家 物種 排放 碳 環境 冰川 生態 化石 NASA 暖化 考古",
        "ko": "기후 과학자 연구진 배출 탄소 환경 빙하 생태 화석 NASA 온난화 고고학",
    },
    "health": {
        "en": "health, hospital, disease, virus, vaccine, cancer, patient, doctor, NHS, outbreak, covid, medical, drug, dementia, obesity, mental health, nurse, pandemic",
        "zh": "健康 醫院 疾病 病毒 疫苗 癌 病患 醫師 醫生 疫情 醫療 藥物 感染 衛生 失智",
        "ko": "건강 병원 질병 바이러스 백신 환자 의사 감염 의료 약물 보건 치매 암 진단",
    },
    "politics": {
        "en": "president, prime minister, election, minister, government, diplomat, summit, sanction, talks, military, troops, war, missile, airstrike, air strike, ceasefire, NATO, UN, treaty, opposition, vote, coup, army, drone, parliament, congress, senate, white house, kremlin, invasion, hamas, hezbollah, houthi, senator, democrat, republican, trump, putin, zelensky, xi jinping, netanyahu, starmer, kremlin, IRGC, foreign policy",
        "zh": "總統 總理 首相 選舉 國會 議會 外交 部長 政府 峰會 制裁 會談 軍 部隊 戰爭 飛彈 導彈 空襲 停火 北約 聯合國 條約 在野 投票 政變 無人機 衝突 白宮 克里姆林宮 川普 普丁 習近平 澤倫斯基 內塔尼亞胡 立法院 立委 國務卿 外長 革命衛隊",
        "ko": "대통령 총리 선거 의회 외교 장관 정부 정상회담 제재 회담 군사 병력 전쟁 미사일 공습 휴전 나토 유엔 조약 야당 투표 쿠데타 드론 분쟁 백악관 크렘린 트럼프 푸틴 시진핑 젤렌스키 네타냐후 국무장관 의원 혁명수비대 외무장관",
    },
    "society": {
        "en": "police, arrest, court, trial, murder, crash, fire, wildfire, flood, earthquake, shooting, stabbing, sentenced, prison, accident, victims, school, students, protest, typhoon, storm, missing, charged, pleads, guilty, rape, killings, investigation, curfew",
        "zh": "警方 警察 逮捕 法院 審判 謀殺 殺害 墜機 車禍 火災 大火 洪水 地震 槍擊 刺傷 判刑 監獄 事故 罹難 學校 抗議 示威 颱風 起訴 嫌犯 犯嫌 被捕 遇襲 宵禁 緊急狀態 判決 涉嫌",
        "ko": "경찰 체포 법원 재판 살해 살인 추락 사고 화재 홍수 지진 총격 흉기 선고 교도소 학교 시위 태풍 기소 용의자 실종 혐의 수사",
    },
    "culture": {
        "en": "film, movie, actor, actress, music, singer, k-pop, festival, museum, art, football, olympic, world cup, tournament, player, coach, award, oscar, novel, celebrity, concert, album, cricket, tennis, baseball, basketball",
        "zh": "電影 演員 音樂 歌手 演唱會 藝術 博物館 足球 棒球 奧運 世界盃 球員 球隊 冠軍 小說 籃球 網球 展覽",
        "ko": "영화 배우 음악 가수 콘서트 K팝 케이팝 예술 박물관 축구 야구 올림픽 월드컵 선수 우승 소설 공연 축제 전시",
    },
}

#: When two kinds score the same, the more specific one wins (war words are in many stories).
_TIE_ORDER = ("dev", "tech", "health", "science", "economy", "culture", "society", "politics")
TITLE_WEIGHT = 3
MIN_SCORE = 3
BODY_CHARS = 2_000


def _compile(words: str, lang: str) -> list[re.Pattern]:
    items = [w.strip() for w in (words.split(",") if lang == "en" else words.split()) if w.strip()]
    patterns = []
    for word in items:
        if lang != "en":
            patterns.append(re.compile(re.escape(word)))
        elif word.isupper():
            patterns.append(re.compile(r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])"))
        else:
            patterns.append(re.compile(r"(?<![A-Za-z])" + re.escape(word), re.IGNORECASE))
    return patterns


_PATTERNS = {
    genre: {lang: _compile(words, lang) for lang, words in by_lang.items()} for genre, by_lang in _WORDS.items()
}
_HANGUL = re.compile(r"[가-힣]")
_HAN = re.compile(r"[一-鿿]")


def _language(text: str) -> str:
    hangul, han = len(_HANGUL.findall(text)), len(_HAN.findall(text))
    if hangul > 10 and hangul >= han:
        return "ko"
    if han > 10:
        return "zh"
    return "en"


def from_section(source: str, url: str, section: str) -> str:
    """The kind the outlet's own section names, or "" when it names none (or only a region)."""
    section = (section or "").strip().lower()
    if source == "BBC":
        return _BBC_SECTIONS.get(section, "")
    if source == "Yonhap":
        return _YONHAP_SECTIONS.get(section, "")
    if source == "CNA":
        match = re.match(r"/news/([a-z]+)/", urlsplit(url or "").path)
        return _CNA_CODES.get(match.group(1), "") if match else ""
    if source == "NAVER NEWS":
        return _NAVER_SECTIONS.get(section, "")
    return ""


def from_words(title: str, paragraphs: list[str]) -> str:
    body = " ".join(paragraphs)[:BODY_CHARS]
    lang = _language(title + " " + body)
    scores: dict[str, int] = {}
    for genre, by_lang in _PATTERNS.items():
        patterns = by_lang.get(lang, [])
        score = sum(TITLE_WEIGHT for p in patterns if p.search(title)) + sum(1 for p in patterns if p.search(body))
        if score:
            scores[genre] = score
    if not scores:
        return ""
    best = max(scores.values())
    if best < MIN_SCORE:
        return ""
    return next(genre for genre in _TIE_ORDER if scores.get(genre) == best)


#: Outlets that only cover technology: their articles are 開発・ネットワーク or テクノロジー unless the
#: words point to economy, science or health (a chip maker's results, say).
TECH_OUTLETS = {"InfoQ": "dev", "The New Stack": "dev", "iThome": "tech", "ZDNet Korea": "tech"}
_TECH_KINDS = ("dev", "tech", "economy", "science", "health")


def classify(source: str, url: str, section: str, title: str, paragraphs: list[str]) -> str:
    if source in TECH_OUTLETS:
        found = from_words(title, paragraphs)
        return found if found in _TECH_KINDS else TECH_OUTLETS[source]
    return from_section(source, url, section) or from_words(title, paragraphs)


_META_SECTION = re.compile(
    rb'<meta[^>]+(?:property="article:section"[^>]*content="([^"]*)"|content="([^"]*)"[^>]*property="article:section")',
    re.I,
)
_NAVER_SECTION = re.compile(rb'name="napp-site-analysis"[^>]*content="[^"]*section=(\d+)', re.I)


def section_of(html: bytes) -> str:
    """The outlet's section as the page states it (``article:section``, NAVER's section id)."""
    head = html[:400_000]
    match = _META_SECTION.search(head)
    if match:
        raw = (match.group(1) or match.group(2) or b"").decode("utf-8", "replace")
        return raw.replace("&amp;", "&").strip()
    match = _NAVER_SECTION.search(head)
    return match.group(1).decode("ascii") if match else ""
