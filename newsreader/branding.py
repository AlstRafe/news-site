"""The app's display name. Only what people see changes with it: the Windows task name, the
credential-store service, file names and browser storage keys keep their original
"NewsHtmlReader" / "news-html-reader" identifiers so existing installations carry on."""
APP_NAME = "NewsBridge"
APP_NAME_JA = "ニュースブリッジ"
APP_TAGLINE = "翻訳して読み比べるニュース"

#: The language of each outlet, for colour-coding its badge (en / zh / ko). Unknown: "".
SOURCE_LANGUAGES = {"BBC": "en", "CNA": "zh", "NAVER NEWS": "ko"}


def source_language(source: str | None) -> str:
    return SOURCE_LANGUAGES.get(source or "", "")
