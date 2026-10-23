"""The app's display name. Only what people see changes with it: the Windows task name, the
credential-store service, file names and browser storage keys keep their original
"NewsHtmlReader" / "news-html-reader" identifiers so existing installations carry on."""
APP_NAME = "NewsBridge"
APP_NAME_JA = "ニュースブリッジ"
APP_TAGLINE = "翻訳して読み比べるニュース"

#: The language of each outlet, for colour-coding its badge (en / zh / ko). Unknown: "".
SOURCE_LANGUAGES = {"BBC": "en", "CNA": "zh", "Yonhap": "ko", "NAVER NEWS": "ko",
                    "InfoQ": "en", "The New Stack": "en", "iThome": "zh", "ZDNet Korea": "ko"}


#: How the outlets are introduced to a Japanese reader (the page footer): name and where it reports from.
OUTLET_INTRO = (("BBC", "英国"), ("中央社", "台湾"), ("聯合ニュース", "韓国"), ("InfoQ", "米国"),
                ("The New Stack", "米国"), ("iThome", "台湾"), ("ZDNet Korea", "韓国"))


def source_language(source: str | None) -> str:
    return SOURCE_LANGUAGES.get(source or "", "")


#: BCP 47 tags for an outlet's language: marks original headlines and text (correct glyphs for
#: Traditional Chinese, Korean line breaking, screen-reader pronunciation).
LANG_TAGS = {"en": "en", "zh": "zh-Hant", "ko": "ko"}


def lang_attr(source: str | None):
    """`` lang="…"`` for an element holding an outlet's original text, or nothing."""
    from markupsafe import Markup
    tag = LANG_TAGS.get(source_language(source), "")
    return Markup(f' lang="{tag}"') if tag else Markup("")
