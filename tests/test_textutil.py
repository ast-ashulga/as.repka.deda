from repka.textutil import html_escape, neutralize_fence, printable


def test_printable_strips_control():
    assert printable("a\x1b[31mb\x07") == "a[31mb"


def test_html_escape():
    assert html_escape('<a href="x">&</a>') == "&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;"


def test_neutralize_fence_leaves_no_triple_quote():
    for text in ('"""', '""""', '"' * 7, 'a """ b """\n"""'):
        out = neutralize_fence(text)
        assert '"""' not in out
        assert out.replace("\u200b", "") == text
    assert neutralize_fence('say "hi" and ""') == 'say "hi" and ""'
