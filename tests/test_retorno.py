"""Testes do combinador de Retorno (pagamento/SISPAG)."""
import sys
from pathlib import Path

from cnab240.retorno import combinar, gravar

CFG = {"separar_por_empresa": True, "ordenar_por_data": True,
       "empresas": ["ALFAMETAL", "BETAVIARIA"]}


def _mk(caminho, empresa, data="24072026", n_lotes=2, banco="341"):
    """Monta um retorno CNAB240 sintético: header arq + lotes + trailer arq."""
    h0 = (banco + "0000" + "0").ljust(102) + empresa.ljust(30)
    h0 = h0.ljust(143) + data
    linhas = [h0.ljust(240)]
    for lote in range(1, n_lotes + 1):
        ln = f"{lote:04d}"
        linhas.append((banco + ln + "1").ljust(240))                 # header lote
        linhas.append((banco + ln + "3" + "00001" + "J").ljust(240))  # detalhe J
        linhas.append((banco + ln + "5").ljust(240))                 # trailer lote
    t9 = (banco + "9999" + "9").ljust(17) + "000000000000000000"
    linhas.append(t9.ljust(240))
    Path(caminho).write_text("\r\n".join(linhas) + "\r\n", encoding="latin-1")
    return Path(caminho)


def test_combina_renumera_lotes_e_recalcula_trailer(tmp_path):
    a = _mk(tmp_path / "d24.ret", "BETAVIARIA TRANSPORTES", "24072026", n_lotes=2)
    b = _mk(tmp_path / "d25.ret", "BETAVIARIA TRANSPORTES", "25072026", n_lotes=3)
    r = combinar([a, b], CFG)

    assert r.ok
    assert r.total_arquivos == 2
    assert r.total_lotes == 5                       # 2 + 3
    # 1 header + (5 lotes * 3 linhas) + 1 trailer = 17
    assert r.total_registros == 17
    # 1 header arquivo, 1 trailer arquivo
    assert sum(1 for l in r.linhas_finais if l[7:8] == "0") == 1
    assert sum(1 for l in r.linhas_finais if l[7:8] == "9") == 1
    # lotes renumerados 0001..0005
    hl = [l[3:7] for l in r.linhas_finais if l[7:8] == "1"]
    assert hl == ["0001", "0002", "0003", "0004", "0005"]
    # trailer recalculado bate com a contagem real
    t9 = [l for l in r.linhas_finais if l[7:8] == "9"][0]
    assert int(t9[17:23]) == 5
    assert int(t9[23:29]) == len(r.linhas_finais)


def test_ordena_por_data_do_header(tmp_path):
    # passa fora de ordem — deve sair cronológico (24 antes de 25)
    b = _mk(tmp_path / "d25.ret", "BETAVIARIA", "25072026", n_lotes=1)
    a = _mk(tmp_path / "d24.ret", "BETAVIARIA", "24072026", n_lotes=1)
    r = combinar([b, a], CFG)
    # o header do combinado é o do arquivo mais antigo (24/07)
    assert r.linhas_finais[0][143:151] == "24072026"


def test_separa_empresa_remove_intruso(tmp_path):
    files = [
        _mk(tmp_path / "t1.ret", "BETAVIARIA TRANSPORTES", "24072026"),
        _mk(tmp_path / "t2.ret", "BETAVIARIA TRANSPORTES", "25072026"),
        _mk(tmp_path / "t3.ret", "BETAVIARIA TRANSPORTES", "26072026"),
        _mk(tmp_path / "f1.ret", "ALFAMETAL S A METAIS",   "25072026"),
    ]
    r = combinar(files, CFG)
    assert r.empresa_predominante == "BETAVIARIA"
    assert r.total_arquivos == 3
    assert any("f1.ret" in x for x in r.arquivos_fora_empresa)


def test_empate_nao_remove(tmp_path):
    files = [
        _mk(tmp_path / "t.ret", "BETAVIARIA TRANSPORTES", "24072026"),
        _mk(tmp_path / "f.ret", "ALFAMETAL S A METAIS",   "25072026"),
    ]
    r = combinar(files, CFG)
    assert r.arquivos_fora_empresa == []
    assert any("mpate" in a for a in r.avisos)
    assert r.total_arquivos == 2


def test_ignora_arquivo_estrutura_invalida(tmp_path):
    bom = _mk(tmp_path / "bom.ret", "BETAVIARIA", "24072026", n_lotes=2)
    ruim = tmp_path / "ruim.ret"
    ruim.write_text("linha qualquer sem estrutura cnab\r\n", encoding="latin-1")
    r = combinar([bom, ruim], CFG)
    assert r.total_arquivos == 1
    assert any("ruim.ret" in x for x in r.arquivos_invalidos)


def test_gravar_crlf_latin1(tmp_path):
    a = _mk(tmp_path / "d24.ret", "BETAVIARIA", "24072026", n_lotes=2)
    r = combinar([a], CFG)
    saida = tmp_path / "combinado.RET"
    gravar(r, saida)
    raw = saida.read_bytes()
    assert raw.count(b"\r\n") == len(r.linhas_finais)
    assert raw.decode("latin-1").splitlines() == r.linhas_finais


def test_banco_misturado_ignora_minoria(tmp_path):
    a = _mk(tmp_path / "i1.ret", "BETAVIARIA", "24072026", banco="341")
    b = _mk(tmp_path / "i2.ret", "BETAVIARIA", "25072026", banco="341")
    c = _mk(tmp_path / "b1.ret", "BETAVIARIA", "25072026", banco="237")
    r = combinar([a, b, c], CFG)
    assert r.total_arquivos == 2                    # só os 2 do Itaú
    assert any("b1.ret" in x for x in r.arquivos_invalidos)
    # todas as linhas do combinado são do mesmo banco
    assert all(l[:3] == "341" for l in r.linhas_finais)


def test_lote_sem_trailer_e_invalido(tmp_path):
    p1 = tmp_path / "trunc.ret"
    banco = "341"
    linhas = [
        (banco + "00000").ljust(102) + "BETAVIARIA".ljust(30),
        (banco + "00011").ljust(240),
        (banco + "00013" + "00001" + "J").ljust(240),
        # SEM trailer de lote (tipo 5) — arquivo truncado
        (banco + "99999").ljust(240),
    ]
    p1.write_text("\r\n".join(linhas) + "\r\n", encoding="latin-1")
    bom = _mk(tmp_path / "bom.ret", "BETAVIARIA", "24072026")
    r = combinar([bom, p1], CFG)
    assert r.total_arquivos == 1
    assert any("trunc.ret" in x for x in r.arquivos_invalidos)


def test_arquivo_de_cobranca_e_rejeitado(tmp_path):
    """RET de cobrança (segmento G) selecionado por engano não entra."""
    p1 = tmp_path / "cobranca.ret"
    banco = "341"
    linhas = [
        (banco + "00000").ljust(102) + "BETAVIARIA".ljust(30),
        (banco + "00011").ljust(240),
        (banco + "00013" + "00001" + "G").ljust(240),
        (banco + "00015").ljust(240),
        (banco + "99999").ljust(240),
    ]
    p1.write_text("\r\n".join(linhas) + "\r\n", encoding="latin-1")
    bom = _mk(tmp_path / "bom.ret", "BETAVIARIA", "24072026")
    r = combinar([bom, p1], CFG)
    assert r.total_arquivos == 1
    assert any("cobranca.ret" in x for x in r.arquivos_invalidos)
