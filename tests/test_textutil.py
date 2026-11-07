from repka.textutil import html_escape, printable


def test_printable_strips_control():
    assert printable("a\x1b[31mb\x07") == "a[31mb"


def test_html_escape():
    assert html_escape('<a href="x">&</a>') == "&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;"
