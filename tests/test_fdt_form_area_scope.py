from pathlib import Path


def test_form_area_is_first_and_scopes_olt_options():
    template = (Path(__file__).resolve().parents[1] / "templates" / "fdt_coding.html").read_text()
    form_start = template.index('<section class="fdt-form"')
    form_end = template.index('</section>', form_start)
    form = template[form_start:form_end]

    assert form.index('id="areaArea"') < form.index('id="oltInitial"')
    assert '<option value="" selected>Pilih Area</option>' in form
    assert 'let fdtFormOltOptions = [];' in template
    assert 'return fdtFormOltOptions.find(o=>o.olt_initial===els.fields.oltInitial.value) || null;' in template
    assert 'async function loadFormOlts(' in template
    assert "new URLSearchParams({area})" in template
    assert "els.fields.areaArea.onchange=()=>{ loadFormOlts().catch" in template
    assert "els.fields.oltInitial.disabled=true;" in template
    assert "if(!area){" in template


def test_toolbar_and_form_olt_state_are_separate():
    template = (Path(__file__).resolve().parents[1] / "templates" / "fdt_coding.html").read_text()
    assert "window.fdtOltOptions = data.olts || [];" in template
    assert "fdtFormOltOptions=data.olts||[];" in template
    assert "if(!els.fields.areaArea.value) throw new Error('AREA wajib dipilih');" in template
    assert "if(!els.fields.oltInitial.value) throw new Error('OLT wajib dipilih');" in template
    assert "const originalLoadSummary" not in template
    assert "window.loadSummary = function" not in template
    assert "updateHubFromOlt();\n  renderOltList();" not in template
