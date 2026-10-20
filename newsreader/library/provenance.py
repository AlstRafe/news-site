"""情報の出どころ (D-73): how an article was reported, read from its own text.

* the dateline: a correspondent abroad (中央社「記者 … 東京19日專電」, 聯合「(도쿄=연합뉴스) … 특파원」),
  a desk reporter at home, or foreign wires compiled at the desk (中央社「綜合外電報導」);
* the other news organisations the article cites ("according to Reuters", 「法新社報導」,
  「교도통신에 따르면」) or only names;
* signs of its own reporting ("told the BBC", 「接受中央社訪問」, 「연합뉴스와 통화에서」).

Nothing is translated or fetched: patterns over the saved text only. A name next to a reporting
verb counts as cited; a name without one ("CNN was barred from the White House") only as named.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# --- organisations ---------------------------------------------------------------

#: Korean particles that may follow a name written in Hangul.
_KO_END = r"(?=[은는이가을를의와과에도로]|[\s,.·)\"'”’]|$)"


@dataclass(frozen=True)
class Org:
    key: str
    label: str      # Japanese
    pattern: str    # English | Korean | Chinese variants


ORGS = [
    Org("reuters", "ロイター", r"Reuters|로이터|路透"),
    Org("ap", "AP通信", r"Associated Press|(?<![A-Za-z])AP(?![A-Za-z])|美聯社|美联社"),
    Org("afp", "AFP通信", r"(?<![A-Za-z])AFP(?![A-Za-z])|Agence France-Presse|法新社"),
    Org("yonhap", "聯合ニュース", r"Yonhap|연합뉴스|韓聯社|韩联社"),
    Org("cna", "中央社", r"Taiwan's Central News Agency|中央社|대만\s?중앙통신"),
    Org("bbc", "BBC", r"(?<![A-Za-z])BBC(?![A-Za-z])|英國廣播公司|英国广播公司|영국\s?(?:공영\s?)?방송"),
    Org("kyodo", "共同通信", r"Kyodo|교도\s?통신|共同社"),
    Org("jiji", "時事通信", r"Jiji|지지\s?통신|時事通信"),
    Org("nhk", "NHK", r"(?<![A-Za-z])NHK(?![A-Za-z])"),
    Org("xinhua", "新華社", r"Xinhua|신화\s?(?:통신|사)|新華社|新华社"),
    Org("kcna", "朝鮮中央通信", r"(?<![A-Za-z])KCNA(?![A-Za-z])|Korean Central News Agency|조선중앙통신|朝中社|朝鮮中央通訊社"),
    Org("tass", "タス通信", r"(?<![A-Za-z])Tass(?![A-Za-z])|(?<![A-Za-z])TASS(?![A-Za-z])|타스\s?통신|(?<![가-힣])타스" + _KO_END + r"|塔斯社"),
    Org("ria", "RIAノーボスチ", r"RIA Novosti|리아\s?노보스티|俄新社"),
    Org("interfax", "インタファクス", r"Interfax|인테르팍스|國際文傳電訊社"),
    Org("bloomberg", "ブルームバーグ", r"Bloomberg|블룸버그|彭博"),
    Org("nyt", "ニューヨーク・タイムズ", r"New York Times|뉴욕\s?타임스|(?<![A-Za-z])NYT(?![A-Za-z])|紐約時報|紐時"),
    Org("wsj", "ウォール・ストリート・ジャーナル", r"Wall Street Journal|월스트리트\s?저널|(?<![A-Za-z])WSJ(?![A-Za-z])|華爾街日報"),
    Org("wapo", "ワシントン・ポスト", r"Washington Post|워싱턴\s?포스트|華盛頓郵報"),
    Org("ft", "フィナンシャル・タイムズ", r"Financial Times|파이낸셜\s?타임스|金融時報|(?<=told the )FT(?![A-Za-z])"),
    Org("cnn", "CNN", r"(?<![A-Za-z])CNN(?![A-Za-z])|有線電視新聞網"),
    Org("cbs", "CBS", r"(?<![A-Za-z])CBS(?![A-Za-z])|哥倫比亞廣播公司"),
    Org("nbc", "NBC", r"(?<![A-Za-z])NBC(?![A-Za-z])|國家廣播公司"),
    Org("abc", "ABC", r"(?<![A-Za-z])ABC(?![A-Za-z])|美國廣播公司"),
    Org("fox", "FOXニュース", r"Fox News|폭스\s?뉴스|福斯新聞|霍士新聞"),
    Org("aljazeera", "アルジャジーラ", r"Al Jazeera|알자지라|半島電視台"),
    Org("guardian", "ガーディアン", r"(?<![A-Za-z])Guardian(?! Council)(?![A-Za-z])|가디언|衛報"),
    Org("politico", "ポリティコ", r"Politico|폴리티코|政治新聞網"),
    Org("axios", "アクシオス", r"Axios|악시오스"),
    Org("thehill", "ザ・ヒル", r"The Hill(?![A-Za-z])|더\s?힐" + _KO_END + r"|國會山莊報"),
    Org("nikkei", "日本経済新聞", r"Nikkei|닛케이|니혼게이자이|日經|日本經濟新聞"),
    Org("yomiuri", "読売新聞", r"Yomiuri|요미우리|讀賣"),
    Org("asahi", "朝日新聞", r"Asahi Shimbun|아사히\s?신문|朝日新聞"),
    Org("mainichi", "毎日新聞", r"Mainichi|마이니치|每日新聞"),
    Org("sankei", "産経新聞", r"Sankei|산케이|產經"),
    Org("globaltimes", "環球時報", r"Global Times|환구시보|環球時報|环球时报"),
    Org("peoplesdaily", "人民日報", r"People's Daily|인민일보|人民日報|人民日报"),
    Org("rodong", "労働新聞", r"Rodong Sinmun|노동신문|勞動新聞|劳动新闻"),
    Org("scmp", "サウスチャイナ・モーニングポスト", r"South China Morning Post|사우스차이나모닝포스트|(?<![A-Za-z])SCMP(?![A-Za-z])|南華早報"),
    Org("chosun", "朝鮮日報", r"Chosun Ilbo|조선일보|朝鮮日報"),
    Org("joongang", "中央日報", r"JoongAng|중앙일보|中央日報"),
    Org("dpa", "dpa通信", r"(?<![A-Za-z])dpa(?![A-Za-z])|(?<![A-Za-z])DPA(?![A-Za-z])|德新社"),
    Org("ansa", "ANSA通信", r"(?<![A-Za-z])ANSA(?![A-Za-z])|安莎社"),
    Org("irna", "IRNA通信", r"(?<![A-Za-z])IRNA(?![A-Za-z])|이르나|伊朗國家通訊社"),
    Org("anadolu", "アナドル通信", r"Anadolu|아나돌루|安納杜魯"),
]
_ORG_RE = re.compile("|".join(f"(?P<{org.key}>{org.pattern})" for org in ORGS))
_ORG_BY_KEY = {org.key: org for org in ORGS}


def _triggers(pattern: str) -> tuple[str, ...]:
    """The leading literal of every alternative ("교도\\s?통신" -> "교도"): a cheap substring test
    decides which organisations' patterns are worth running on a paragraph."""
    out = []
    for alternative in re.split(r"\|(?![^(]*\))", pattern):
        bare = re.sub(r"\(\?<?[!=][^)]*\)", "", alternative)
        literal = re.match(r"[^\\()\[\]?*+|{}^$]+", bare)
        if literal:
            out.append(literal.group(0).rstrip())
    return tuple(t for t in out if t)


_SCAN = [(org.key, _triggers(org.pattern), re.compile(org.pattern)) for org in ORGS]


def _mentions(paragraph: str):
    """(key, start, end) of every organisation named in the paragraph, in text order."""
    found = []
    for key, triggers, regex in _SCAN:
        if any(t in paragraph for t in triggers):
            found += [(key, m.start(), m.end()) for m in regex.finditer(paragraph)]
    found.sort(key=lambda f: f[1])
    return found

#: The outlets this library collects, by their source name (self-mentions are not citations).
SELF = {"BBC": "bbc", "CNA": "cna", "Yonhap": "yonhap"}

# A reporting word just before or after a name makes it a citation. English allows a few words
# between the verb and the name ("told news agency Reuters", "according to the BBC's US partner CBS").
_CUE_BEFORE_EN = re.compile(r"\b(?:according to|told|tells|telling|reported by|reports|reported|citing|cited by|quoted by|"
                            r"quoting|interview with|interview on|speaking to|spoke to|comments to|statement to|"
                            r"obtained by|wrote in|writing in|via)(?:\s+[\w'’.-]+){0,4}\s*$"
                            r"|\b(?:confirmed|revealed|reveal|described|explained|said)(?:\s+[\w'’.-]+){0,3}\s+to\s*$",
                            re.IGNORECASE)
_CUE_BEFORE_ZH = re.compile(r"(?:據|根據|綜合|综合|告訴|接受|引述|援引|轉述|引用|向|對|給|回應|回覆)[^，。、；]{0,6}$")
_CUE_AFTER_EN = re.compile(r"^[^.!?]{0,40}?\b(?:reported|reports|reporting|report|said|says|cited|quoted|revealed|obtained|"
                           r"analysis|poll|survey|investigation|news agency)\b", re.IGNORECASE)
_CUE_AFTER_CJK = re.compile(r"^[^。！？]{0,24}?(?:報導|报道|報道|指出|披露|引述|分析|稱|表示|消息|調查|民調|取得|獲得|專訪|"
                            r"訪問|認為|舉例|提到|證實|無法|置評|查詢|記者|說法|따르면|인용|논평|질의|인터뷰|통화|여론조사)")
#: 「華盛頓郵報」（The Washington Post）報導: the quote mark and the original name do not count as distance.
_ASIDE = re.compile(r"^[」”\"]?\s?[（(][^）)]{0,40}[）)]|^[」”\"]")
# Korean: "로이터통신은 … 보도했다", "블룸버그·로이터 통신이 보도했다" (the name as the subject of a
# reporting verb), and "더힐에 … 말했다" (told The Hill).
_KO_SUBJECT = re.compile(r"^(?:[·,]\s?[^\s·,]{1,12})*(?:\s?통신|\([A-Za-z]+\))?(?:은|는|이|가|도)\s(?P<rest>[^.。]{0,300})")
_KO_TOLD = re.compile(r"^(?:\s?통신)?(?:에|에게)\s(?P<rest>[^.。]{0,200})")
_KO_REPORTING = re.compile(r"보도|전했|밝혔|분석했|지적했|설명했|해설했|평가했|평했|진단했|내다봤|전망했|덧붙였|보냈|썼다|소개했|"
                           r"주장했|꼬집었|추산했|추정했|집계했|비판했|주목했|예상했|관측했|짚었|논평했|인용했|확인했|입수했|공개했|폭로했")
_KO_SAID = re.compile(r"말했|밝혔|전했|설명했|털어놨")


def _cited(text: str, start: int, end: int) -> bool:
    before, after = text[max(0, start - 50):start], text[end:end + 300]
    after = _ASIDE.sub("", after, count=1)
    if _CUE_BEFORE_EN.search(before) or _CUE_BEFORE_ZH.search(before[-12:]):
        return True
    if _CUE_AFTER_EN.search(after) or _CUE_AFTER_CJK.search(after):
        return True
    subject = _KO_SUBJECT.match(after)
    if subject and _KO_REPORTING.search(subject["rest"]):
        return True
    told = _KO_TOLD.match(after)
    return bool(told and _KO_SAID.search(told["rest"]))


# --- the outlet's own reporting -------------------------------------------------------

_OWN = {
    "bbc": [(re.compile(r"(?:told|tells|telling|speaking to|spoke to|spoken to|interview with|said to)\s(?:the\s)?BBC"), "独自インタビュー"),
            (re.compile(r"BBC Verify"), "BBC Verify の検証"),
            (re.compile(r"BBC (?:has |had )?(?:seen|obtained|analysed|analyzed|verified|established|confirmed)"), "BBC が独自に確認")],
    "cna": [(re.compile(r"(?:接受|告訴|向|對|回覆|回應)中央社(?:記者)?|中央社(?:記者)?(?:訪問|專訪|採訪|詢問|查證)"), "独自取材")],
    "yonhap": [(re.compile(r"연합뉴스(?:와의|와|에|에게)\s*(?:전화\s*|서면\s*)?(?:인터뷰|통화|말했|밝혔|전했)"
                           r"|연합뉴스(?:가|는)\s*(?:입수|취재|확인)"), "独自取材")],
}

# --- datelines ---------------------------------------------------------------------

_CNA_DATELINE = re.compile(r"^[（(]中央社(?:記者\s*(?P<reporter>[^）)]*?)\s+)?(?P<place>[^\s\d（）()]{1,30}?)\s*\d{1,2}日"
                           r"(?:\d{1,2}時)?\s*(?P<kind>專電|綜合外電報導|外電報導|綜合報導|電)[）)]")
_YONHAP_DATELINE = re.compile(r"^\((?P<place>[^=()]{1,12})=연합뉴스\)\s*(?:[^=]*?(?P<kind>특파원|기자))?[^=]*=")

#: Dateline places as written by CNA (Traditional Chinese) and Yonhap (Hangul), in Japanese.
PLACES = {
    "台北": "台北", "臺北": "台北", "華盛頓": "ワシントン", "東京": "東京", "巴黎": "パリ", "基輔": "キーウ", "首爾": "ソウル",
    "倫敦": "ロンドン", "雪梨": "シドニー", "梵蒂岡": "バチカン", "利雅德": "リヤド", "曼谷": "バンコク", "杜拜": "ドバイ",
    "布魯塞爾": "ブリュッセル", "吉隆坡": "クアラルンプール", "德黑蘭": "テヘラン", "新德里": "ニューデリー",
    "維爾紐斯": "ビリニュス", "紐約": "ニューヨーク", "莫斯科": "モスクワ", "新加坡": "シンガポール",
    "斯德哥爾摩": "ストックホルム", "洛杉磯": "ロサンゼルス", "布拉格": "プラハ", "奈洛比": "ナイロビ", "索菲亞": "ソフィア",
    "華沙": "ワルシャワ", "雅加達": "ジャカルタ", "日內瓦": "ジュネーブ", "馬尼拉": "マニラ", "開羅": "カイロ",
    "舊金山": "サンフランシスコ", "貝爾法斯特": "ベルファスト", "北京": "北京", "上海": "上海", "香港": "香港",
    "柏林": "ベルリン", "羅馬": "ローマ", "耶路撒冷": "エルサレム", "特拉維夫": "テルアビブ", "河內": "ハノイ",
    "胡志明市": "ホーチミン", "大阪": "大阪", "釜山": "釜山", "平壤": "平壌", "渥太華": "オタワ", "坎培拉": "キャンベラ",
    "墨西哥城": "メキシコ市", "聖保羅": "サンパウロ", "伊斯坦堡": "イスタンブール", "安卡拉": "アンカラ", "多哈": "ドーハ",
    "巴格達": "バグダッド", "喀布爾": "カブール", "伊斯蘭堡": "イスラマバード", "仰光": "ヤンゴン", "金邊": "プノンペン",
    "威靈頓": "ウェリントン", "芝加哥": "シカゴ", "哥本哈根": "コペンハーゲン", "奧斯陸": "オスロ", "赫爾辛基": "ヘルシンキ",
    "維也納": "ウィーン", "馬德里": "マドリード", "里斯本": "リスボン", "阿姆斯特丹": "アムステルダム", "海牙": "ハーグ",
    "雅典": "アテネ", "布達佩斯": "ブダペスト", "聯合國": "国連", "台中": "台中", "高雄": "高雄", "台南": "台南",
    "新竹": "新竹", "花蓮": "花蓮", "桃園": "桃園", "宜蘭": "宜蘭", "嘉義": "嘉義", "屏東": "屏東", "金門": "金門",
    "워싱턴": "ワシントン", "카이로": "カイロ", "런던": "ロンドン", "샌프란시스코": "サンフランシスコ", "멕시코시티": "メキシコ市",
    "바티칸": "バチカン", "뉴욕": "ニューヨーク", "베를린": "ベルリン", "베이징": "北京", "도쿄": "東京", "서울": "ソウル",
    "파리": "パリ", "브뤼셀": "ブリュッセル", "모스크바": "モスクワ", "상하이": "上海", "홍콩": "香港", "타이베이": "台北",
    "하노이": "ハノイ", "방콕": "バンコク", "자카르타": "ジャカルタ", "뉴델리": "ニューデリー", "시드니": "シドニー",
    "제네바": "ジュネーブ", "로마": "ローマ", "예루살렘": "エルサレム", "이스탄불": "イスタンブール", "두바이": "ドバイ",
    "테헤란": "テヘラン", "키이우": "キーウ", "오사카": "大阪", "부산": "釜山", "평양": "平壌", "오타와": "オタワ",
    "상파울루": "サンパウロ", "로스앤젤레스": "ロサンゼルス", "시카고": "シカゴ", "마드리드": "マドリード", "빈": "ウィーン",
    "프라하": "プラハ", "바르샤바": "ワルシャワ", "쿠알라룸푸르": "クアラルンプール", "싱가포르": "シンガポール",
    "마닐라": "マニラ", "리야드": "リヤド", "요하네스버그": "ヨハネスブルク", "나이로비": "ナイロビ",
    "부에노스아이레스": "ブエノスアイレス", "리우데자네이루": "リオデジャネイロ", "토론토": "トロント", "밴쿠버": "バンクーバー",
    "휴스턴": "ヒューストン", "실리콘밸리": "シリコンバレー", "선양": "瀋陽", "광저우": "広州", "프랑크푸르트": "フランクフルト",
    "암스테르담": "アムステルダム", "스톡홀름": "ストックホルム", "헬싱키": "ヘルシンキ", "오슬로": "オスロ",
    "코펜하겐": "コペンハーゲン", "아테네": "アテネ", "부다페스트": "ブダペスト", "리스본": "リスボン", "유엔": "国連",
    "세종": "世宗", "인천": "仁川", "대전": "大田", "대구": "大邱", "광주": "光州", "수원": "水原", "춘천": "春川",
    "청주": "清州", "전주": "全州", "창원": "昌原", "울산": "蔚山", "제주": "済州", "안동": "安東", "포항": "浦項",
    "판문점": "板門店", "파주": "坡州", "의정부": "議政府", "홍성": "洪城", "무안": "務安", "강릉": "江陵",
    "曼徹斯特": "マンチェスター", "蒙特婁": "モントリオール", "西雅圖": "シアトル", "克里夫蘭": "クリーブランド",
    "伊斯蘭馬巴德": "イスラマバード", "巴拿馬市": "パナマ市", "米蘭": "ミラノ", "地拉那": "ティラナ", "波士頓": "ボストン",
    "休士頓": "ヒューストン", "邁阿密": "マイアミ", "溫哥華": "バンクーバー", "多倫多": "トロント",
    "布宜諾斯艾利斯": "ブエノスアイレス", "里約熱內盧": "リオデジャネイロ", "約翰尼斯堡": "ヨハネスブルク",
    "阿布達比": "アブダビ", "貝魯特": "ベイルート", "大馬士革": "ダマスカス", "安曼": "アンマン", "加薩": "ガザ",
    "巴庫": "バクー", "第比利斯": "トビリシ", "明斯克": "ミンスク", "塔林": "タリン", "里加": "リガ",
    "布加勒斯特": "ブカレスト", "貝爾格勒": "ベオグラード", "都柏林": "ダブリン", "愛丁堡": "エディンバラ",
    "法蘭克福": "フランクフルト", "慕尼黑": "ミュンヘン", "蘇黎世": "チューリッヒ", "伯恩": "ベルン",
    "盧森堡": "ルクセンブルク", "斯特拉斯堡": "ストラスブール", "墨爾本": "メルボルン", "奧克蘭": "オークランド",
    "達卡": "ダッカ", "可倫坡": "コロンボ", "加德滿都": "カトマンズ", "烏蘭巴托": "ウランバートル",
    "海參崴": "ウラジオストク", "永珍": "ビエンチャン", "沖繩": "沖縄", "那霸": "那覇", "福岡": "福岡", "札幌": "札幌",
    "京都": "京都", "廣島": "広島", "長崎": "長崎", "名古屋": "名古屋", "橫濱": "横浜", "神戶": "神戸",
    "이슬라마바드": "イスラマバード", "베이루트": "ベイルート", "앙카라": "アンカラ", "아부다비": "アブダビ", "도하": "ドーハ",
    "바그다드": "バグダッド", "카불": "カブール", "다카": "ダッカ", "양곤": "ヤンゴン", "프놈펜": "プノンペン",
    "울란바토르": "ウランバートル", "블라디보스토크": "ウラジオストク", "비엔티안": "ビエンチャン", "웰링턴": "ウェリントン",
    "캔버라": "キャンベラ", "멜버른": "メルボルン", "오클랜드": "オークランド", "상트페테르부르크": "サンクトペテルブルク",
    "민스크": "ミンスク", "탈린": "タリン", "리가": "リガ", "빌뉴스": "ビリニュス", "부쿠레슈티": "ブカレスト",
    "베오그라드": "ベオグラード", "더블린": "ダブリン", "에든버러": "エディンバラ", "뮌헨": "ミュンヘン", "취리히": "チューリッヒ",
    "베른": "ベルン", "보스턴": "ボストン", "마이애미": "マイアミ", "시애틀": "シアトル", "애틀랜타": "アトランタ",
    "텔아비브": "テルアビブ", "가자": "ガザ", "바쿠": "バクー", "트빌리시": "トビリシ", "하바나": "ハバナ", "보고타": "ボゴタ",
    "리마": "リマ", "산티아고": "サンティアゴ", "카라카스": "カラカス", "브라질리아": "ブラジリア", "아디스아바바": "アディスアベバ",
    "라고스": "ラゴス", "다마스쿠스": "ダマスカス", "암만": "アンマン", "쿠웨이트": "クウェート", "앙골라": "アンゴラ",
}
#: Home bases: a reporter here is at the outlet's own desk, not a correspondent abroad.
HOME = {"cna": {"台北", "臺北", "台中", "高雄", "台南", "新竹", "花蓮", "桃園", "宜蘭", "嘉義", "屏東", "金門"},
        "yonhap": {"서울", "세종", "인천", "대전", "대구", "광주", "수원", "춘천", "청주", "전주", "창원", "울산", "제주",
                   "안동", "포항", "판문점", "파주", "의정부", "홍성", "무안", "강릉", "부산"}}


def place_ja(place: str) -> str:
    """「巴黎／米蘭」 -> 「パリ・米蘭」 (a place not in the table is kept as written)."""
    return "・".join(PLACES.get(part.strip(), part.strip()) for part in re.split(r"[／/、]", place) if part.strip())


def dateline(source: str, first: str) -> dict | None:
    """{"kind": correspondent|staff|wire|compiled, "place", "place_ja", "label"} from an opening paragraph."""
    me = SELF.get(source)
    text = (first or "").lstrip()
    if me == "cna":
        match = _CNA_DATELINE.match(text)
        if not match:
            return None
        place, kind = match["place"], match["kind"]
        if kind in ("綜合外電報導", "外電報導"):
            kind = "wire"
        elif kind == "綜合報導":
            kind = "compiled"
        elif match["reporter"] is not None and not set(re.split(r"[／/、]", place)) <= HOME["cna"]:
            kind = "correspondent"
        else:
            kind = "staff"
    elif me == "yonhap":
        match = _YONHAP_DATELINE.match(text)
        if not match:
            return None
        place = match["place"].strip()
        kind = "correspondent" if match["kind"] == "특파원" or place not in HOME["yonhap"] else "staff"
    else:
        return None
    ja = place_ja(place)
    label = {"correspondent": f"{ja}の特派員", "staff": f"{ja}の記者", "wire": f"外電のまとめ（{ja}発）",
             "compiled": "複数の報道のまとめ"}[kind]
    return {"kind": kind, "place": place, "place_ja": ja, "label": label}


# --- one article ---------------------------------------------------------------------

def analyze(source: str, paragraphs: list[str]) -> dict:
    """{"dateline", "cites": [{"key", "label", "count"}], "named": [...], "own": [labels]}."""
    me = SELF.get(source, "")
    paragraphs = [p for p in (paragraphs or []) if p]
    found: dict[str, dict] = {}
    for paragraph in paragraphs:
        seen_here: set[str] = set()
        for key, start, end in _mentions(paragraph):
            if key == me:
                continue
            entry = found.setdefault(key, {"key": key, "label": _ORG_BY_KEY[key].label, "count": 0, "cited": False,
                                           "order": len(found)})
            if key not in seen_here:
                entry["count"] += 1
                seen_here.add(key)
            if not entry["cited"] and _cited(paragraph, start, end):
                entry["cited"] = True
    ordered = sorted(found.values(), key=lambda e: (-e["count"], e["order"]))
    clean = [{"key": e["key"], "label": e["label"], "count": e["count"]} for e in ordered]
    own = []
    text = "\n".join(paragraphs)
    for pattern, label in _OWN.get(me, []):
        if pattern.search(text) and label not in own:
            own.append(label)
    return {"dateline": dateline(source, paragraphs[0]) if paragraphs else None,
            "cites": [c for c, e in zip(clean, ordered) if e["cited"]],
            "named": [c for c, e in zip(clean, ordered) if not e["cited"]],
            "own": own}


def _kind_label(kind: str, places: list[str], limit: int = 4) -> str:
    """Several articles of one outlet: 「外電のまとめ（キーウ・モスクワ発）」, 「特派員（東京・ソウル）」."""
    shown = "・".join(places[:limit]) + (f" ほか{len(places) - limit}" if len(places) > limit else "")
    return {"correspondent": f"特派員（{shown}）", "staff": f"記者（{shown}）", "wire": f"外電のまとめ（{shown}発）",
            "compiled": "複数の報道のまとめ"}[kind]


def summary_line(info: dict, limit: int = 3) -> str:
    """One short line: 「東京の特派員 · 出典: 共同通信・NHK」."""
    parts = []
    if info.get("dateline"):
        parts.append(info["dateline"]["label"])
    parts += info.get("own", [])[:1]
    cites = info.get("cites") or []
    if cites:
        more = f" ほか{len(cites) - limit}" if len(cites) > limit else ""
        parts.append("出典: " + "・".join(c["label"] for c in cites[:limit]) + more)
    return " · ".join(parts)


# --- a group of articles -----------------------------------------------------------------

def compare(members: list[dict]) -> dict:
    """members: [{"id", "source", "info"}]. Organisations cited by two or more outlets (the
    reports may not be independent), and members citing another outlet of the same group."""
    outlets_in_group = {SELF.get(m["source"]): m["source"] for m in members if SELF.get(m["source"])}
    by_org: dict[str, dict] = {}
    internal: list[dict] = []
    for m in members:
        for cite in m["info"]["cites"]:
            entry = by_org.setdefault(cite["key"], {"label": cite["label"], "outlets": []})
            if m["source"] not in entry["outlets"]:
                entry["outlets"].append(m["source"])
            target = outlets_in_group.get(cite["key"])
            if target and target != m["source"] and not any(i["from"] == m["source"] and i["to"] == target for i in internal):
                internal.append({"from": m["source"], "to": target, "id": m["id"]})
    shared = [{"label": v["label"], "outlets": v["outlets"]} for v in by_org.values() if len(v["outlets"]) > 1]
    shared.sort(key=lambda s: -len(s["outlets"]))
    kinds = {}
    for m in members:
        if m["info"]["dateline"]:
            kinds.setdefault(m["source"], [])
            label = m["info"]["dateline"]["label"]
            if label not in kinds[m["source"]]:
                kinds[m["source"]].append(label)
    own = {}
    for m in members:
        for label in m["info"]["own"]:
            own.setdefault(m["source"], [])
            if label not in own[m["source"]]:
                own[m["source"]].append(label)
    return {"shared": shared, "internal": internal, "datelines": kinds, "own": own,
            "any": bool(shared or internal or kinds or own or by_org)}


# --- reading from the library ------------------------------------------------------------

import threading as _threading
from collections import OrderedDict as _OrderedDict
from contextlib import closing as _closing
from datetime import datetime as _datetime, timedelta as _timedelta, timezone as _timezone
from pathlib import Path as _Path

#: Analyses kept in memory, keyed by (id, mtime, size): an article's text changes only when re-saved.
_CACHE: "_OrderedDict[tuple, dict]" = _OrderedDict()
_CACHE_SIZE = 4000
_CACHE_LOCK = _threading.Lock()


def _cached(row, decode) -> dict:
    key = (row["id"], row["mtime"], row["size"])
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return hit
    content = decode(row["content"], row) if row["content"] is not None else None
    info = analyze(row["source"] or "", (content or {}).get("paragraphs") or [])
    with _CACHE_LOCK:
        _CACHE[key] = info
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return info


def for_ids(db_path: _Path, ids: list[int]) -> dict[int, dict]:
    from newsreader.library import index
    if not ids:
        return {}
    with _closing(index.connect(db_path)) as db:
        rows = db.execute("SELECT id, source, title, url, published, mtime, size, content FROM articles "
                          "WHERE error = '' AND id IN (%s)" % ",".join("?" * len(ids)), tuple(ids)).fetchall()
    return {row["id"]: _cached(row, index.decode_content) for row in rows}


def story(db_path: _Path, members: list[dict]) -> dict:
    """For a group page: a line per article, each outlet's summary, and the notes of compare()."""
    infos = for_ids(db_path, [m["id"] for m in members])
    rows = [{"id": m["id"], "source": m.get("source") or "", "info": infos[m["id"]]} for m in members if m["id"] in infos]
    lines = {row["id"]: summary_line(row["info"]) for row in rows}
    outlets: dict[str, dict] = {}
    for row in rows:
        entry = outlets.setdefault(row["source"], {"source": row["source"], "places": {}, "own": [], "cites": {}})
        info = row["info"]
        if info["dateline"]:
            places = entry["places"].setdefault(info["dateline"]["kind"], [])
            for place in info["dateline"]["place_ja"].split("・"):
                if place and place not in places:
                    places.append(place)
        for label in info["own"]:
            if label not in entry["own"]:
                entry["own"].append(label)
        for cite in info["cites"]:
            entry["cites"][cite["label"]] = entry["cites"].get(cite["label"], 0) + 1
    for e in outlets.values():
        e["datelines"] = [_kind_label(kind, places) for kind, places in e["places"].items()]
    summary = [{"source": e["source"], "datelines": e["datelines"], "own": e["own"],
                "cites": [label for label, _ in sorted(e["cites"].items(), key=lambda kv: -kv[1])]}
               for e in outlets.values() if e["datelines"] or e["own"] or e["cites"]]
    notes = compare(rows)
    return {"lines": {k: v for k, v in lines.items() if v}, "outlets": summary, "shared": notes["shared"],
            "internal": notes["internal"]}


def profiles(db_path: _Path, days: int = 7, now: _datetime | None = None) -> dict[str, dict]:
    """媒体くらべ: per outlet, how its articles were reported and whom they cite most."""
    from newsreader.library import index
    now = (now or _datetime.now(_timezone.utc)).replace(tzinfo=None)
    since = (now - _timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _closing(index.connect(db_path)) as db:
        rows = db.execute("SELECT id, source, title, url, published, mtime, size, content FROM articles "
                          "WHERE error = '' AND source != '' AND published >= ?", (since,)).fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        info = _cached(row, index.decode_content)
        entry = out.setdefault(row["source"], {"articles": 0, "kinds": {}, "own": 0, "cites": {}, "citing": 0})
        entry["articles"] += 1
        if info["dateline"]:
            kind = info["dateline"]["kind"]
            entry["kinds"][kind] = entry["kinds"].get(kind, 0) + 1
        if info["own"]:
            entry["own"] += 1
        if info["cites"]:
            entry["citing"] += 1
        for cite in info["cites"]:
            entry["cites"][cite["label"]] = entry["cites"].get(cite["label"], 0) + 1
    for entry in out.values():
        entry["top_cites"] = sorted(entry["cites"].items(), key=lambda kv: -kv[1])[:5]
        entry["kind_rows"] = [(KIND_LABELS[k], entry["kinds"][k]) for k in KIND_LABELS if entry["kinds"].get(k)]
    return out


KIND_LABELS = {"correspondent": "特派員", "staff": "本社・国内の記者", "wire": "外電のまとめ", "compiled": "複数の報道のまとめ"}
