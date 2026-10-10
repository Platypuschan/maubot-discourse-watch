from discourse_watch.render import render_post

BASE = "https://forum.example.org"


def test_emoji_and_heading_anchor():
    cooked = (
        '<h2><a name="x" class="anchor" href="#x"></a>'
        '<img src="/images/emoji/twitter/star2.png" title=":star2:" class="emoji" alt=":star2:"> '
        "Enhancements</h2>"
    )
    r = render_post(cooked, BASE, 1000)
    assert r.html == "<h2>:star2: Enhancements</h2>"
    assert r.text == ":star2: Enhancements"


def test_relative_links_become_absolute_and_unsafe_dropped():
    cooked = '<p><a class="mention" href="/u/alice">@alice</a> <a href="javascript:alert(1)">x</a></p>'
    r = render_post(cooked, BASE, 1000)
    assert r.html == f'<p><a href="{BASE}/u/alice">@alice</a> x</p>'


def test_lightbox_image_links_full_size():
    cooked = (
        '<div class="lightbox-wrapper"><a class="lightbox" href="/uploads/full.png">'
        '<img src="/uploads/small.png" alt="screen"><div class="meta">screen.png 1 MB</div>'
        "</a></div>"
    )
    r = render_post(cooked, BASE, 1000)
    assert r.html == f'<div><a href="{BASE}/uploads/full.png">🖼 screen</a></div>'


def test_quote_and_onebox():
    cooked = (
        '<aside class="quote" data-username="bob"><div class="title">'
        '<img class="avatar" src="/a.png"> bob:</div><blockquote><p>hi</p></blockquote></aside>'
        '<aside class="onebox" data-onebox-src="https://github.com/x/y"><header>junk</header></aside>'
    )
    r = render_post(cooked, BASE, 1000)
    assert r.html == (
        "<p><em>bob:</em></p><blockquote><p>hi</p></blockquote>"
        '<p><a href="https://github.com/x/y">https://github.com/x/y</a></p>'
    )
    assert "> hi" in r.text


def test_code_language_and_escaping():
    cooked = '<pre><code class="lang-bash">echo "&lt;b&gt;"</code></pre><script>evil()</script>'
    r = render_post(cooked, BASE, 1000)
    assert r.html == '<pre><code class="language-bash">echo "&lt;b&gt;"</code></pre>'
    assert r.text == '```\necho "<b>"\n```'


def test_truncation_closes_tags():
    cooked = "<p>" + "a" * 50 + "</p><p>more</p>"
    r = render_post(cooked, BASE, 10)
    assert r.truncated
    assert r.html == "<p>aaaaaaaaaa</p>"


def test_lists_in_text():
    r = render_post("<ol>\n<li>one</li>\n<li>two</li>\n</ol><ul><li>x</li></ul>", BASE, 1000)
    assert r.text == "1. one\n2. two\n\n• x"
