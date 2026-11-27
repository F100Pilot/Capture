from pagecapture.scoring import rank, score, tokens

VW, VH = 1000, 600


def feat(**kw):
    base = dict(tag="button", role="", text="", aria="", title="", alt="", id="", cls="",
                disabled=False, x=900, y=550, w=80, h=40, vw=VW, vh=VH)
    base.update(kw)
    return base


def test_tokens_camelcase_and_accents():
    assert tokens("btnNext Avançar") == {"btn", "next", "avancar"}
    assert "next" not in tokens("context")


def test_positive_labels_pt_en_accepted():
    for text in ("Next", "Seguinte", "Próximo", "Avançar", "Continuar", "Continue"):
        assert score(feat(text=text)) >= 10, text


def test_negative_labels_rejected():
    for text in ("Previous", "Voltar", "Sair", "Menu", "Anterior", "Fechar"):
        assert score(feat(text=text)) is None, text
    assert score(feat(text="Next", cls="btn-back")) is None


def test_icons_accepted():
    assert score(feat(aria="Seguinte")) >= 10
    assert score(feat(tag="span", text="›")) >= 7
    assert score(feat(tag="div", cls="nav-arrow-right")) >= 6


def test_left_arrow_rejected():
    assert score(feat(tag="div", cls="nav-arrow-left")) is None
    assert score(feat(cls="fa fa-chevron-left")) is None


def test_disabled_ignored():
    assert score(feat(text="Next", disabled=True)) is None


def test_large_container_penalised():
    assert score(feat(tag="div", cls="next-section", w=900, h=500)) < 6


def test_long_text_not_a_label():
    long_text = "Clique em next para continuar a leitura deste capítulo extenso do curso"
    assert len(long_text) > 40
    assert score(feat(tag="div", text=long_text)) == 0


def test_rank_picks_next_in_other_frame():
    top = [feat(text="Menu", x=100, y=20), feat(tag="a", text="Ajuda")]
    content = [feat(text="Previous", x=100), feat(text="Next")]
    ranked = rank([top, content])
    assert ranked and ranked[0].frame_index == 1 and ranked[0].idx == 1
    assert '"Next"' in ranked[0].description
