"""Word-level transcripts from the platform's own caption track.

YouTube auto-captions ship as rolling WebVTT where only the inline-tagged words
carry real timings; the untagged text is the repeat window. These tests pin that
parsing rule and the auto-provider's preference order.
"""

from dataclasses import replace

from clipbait.transcribe.subtitles import is_youtube, parse_vtt

ROLLING_VTT = """WEBVTT
Kind: captions
Language: en

00:00:33.680 --> 00:00:37.350 align:start position:0%
 
>> Only<00:00:34.239><c> love</c><00:00:34.800><c> never</c><00:00:35.280><c> hesitate.</c>

00:00:37.350 --> 00:00:37.360 align:start position:0%
>> Only love never hesitate.
 

00:00:37.360 --> 00:00:40.600 align:start position:0%
>> Only love never hesitate.
Only<00:00:38.079><c> trust</c>
"""


def test_inline_tags_give_word_timings_and_context_is_ignored() -> None:
    words, segments = parse_vtt(ROLLING_VTT)
    assert [word.text for word in words] == ["love", "never", "hesitate.", "trust"]
    assert words[0].start == 34.239
    assert words[0].end == 34.8
    assert words[3].start == 38.079
    # "Only" appears only as untagged context, so it must not be stamped twice.
    assert all(word.text != "Only" for word in words)
    assert segments and segments[0].start == 34.239


def test_duplicate_words_at_the_same_time_are_merged() -> None:
    doc = """WEBVTT

00:00:01.000 --> 00:00:02.000 align:start position:0%
a<00:00:01.000><c> yeah</c>
a<00:00:01.000><c> yeah</c>
"""
    words, _ = parse_vtt(doc)
    assert [word.text for word in words] == ["yeah"]


def test_bracket_and_placeholder_cues_are_dropped() -> None:
    # YouTube writes "[laughter]", "[Music]" and "[ __ ]" for non-speech; burned
    # into a caption they read as broken text, so the parser drops them while
    # keeping the real words around them.
    doc = """WEBVTT

00:00:01.000 --> 00:00:02.000 align:start position:0%
x<00:00:01.500><c> [laughter]</c><00:00:01.700><c> real</c>
x<00:00:01.900><c> [\u00a0__\u00a0]</c><00:00:02.100><c> \u266a</c>
"""
    words, _ = parse_vtt(doc)
    assert [word.text for word in words] == ["real"]


def test_is_youtube_only_matches_video_hosts() -> None:
    assert is_youtube("https://www.youtube.com/watch?v=abc")
    assert is_youtube("https://youtu.be/abc")
    assert not is_youtube("https://www.twitch.tv/videos/1")
    assert not is_youtube(None)


def test_local_sources_do_not_use_the_caption_provider() -> None:
    from clipbait.config import get_settings
    from clipbait.transcribe import AutoTranscriber

    settings = replace(get_settings(), transcriber="auto")
    provider = AutoTranscriber(settings)
    assert provider.resolve().name != "subtitles"


def test_explicit_caption_preference_is_honoured() -> None:
    from clipbait.config import get_settings
    from clipbait.transcribe import AutoTranscriber

    settings = replace(get_settings(), transcriber="youtube")
    assert AutoTranscriber(settings).resolve().name == "subtitles"
