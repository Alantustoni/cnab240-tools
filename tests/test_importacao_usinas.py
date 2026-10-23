"""
test_importacao_usinas.py
Monta o arquivo de importação a partir de um CSV de boletos + molde RET,
conferindo que cada campo cai na posição certa e que a duplicação por
filial funciona (CNPJ matriz -> filiais).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from cnab240 import importacao_usinas as imp

BARRA = "34196157900084176261090000000000000000000001"   # 44 díg
CFG_RET = {
    "trailer_lote_legado":
        "23700025         000032000000000000000000000000000000000000000000",
    "trailer_arquivo_legado": "23799999         000002000108",
}
FILIAIS = {"90100200": ["90100200002586", "90100200006069"]}


def _molde_ret(tmp_path) -> Path:
    """RET sintético (Bradesco) com um título G + complemento H."""
    g = ("2370001" + "3" + "00001" + "G" + " " + "01"
         + "9" * 44                       # [17:61] barra (molde)
         + "2" + "0" * 15                 # [61] tipo + [62:77] inscrição
         + "MOLDE QUALQUER LTDA".ljust(30)[:30]   # [77:107]
         + "01012026"                     # [107:115]
         + f"{100000:015d}"               # [115:130]
         + "0" * 15 + "09"                # [130:147]
         + "MOLDE".ljust(15)[:15])        # [147:162]
    g = g.ljust(240)[:240]
    h = ("2370001" + "3" + "00002" + "H" + " " + "01" + "0" * 200).ljust(240)[:240]
    p = tmp_path / "molde.ret"
    p.write_text("237...HEADER\r\n" + g + "\r\n" + h + "\r\n"
                 + "23799999TR\r\n", encoding="latin-1")
    return p


def _csv(tmp_path, linhas_boleto) -> Path:
    cab = ("Pagador/Agregado;Beneficiário;CPF/CNPJ;Venc.;Nº Doc.;Até Venc.;"
           "A Pagar;Tipo de Boleto;Banco;Observações;Código de barras")
    linhas = [
        ";;;;;;;;;;",
        "Dados da conta;;;;;;;;;;",
        "Nome da empresa: ALFAMETAL S/A;;;;;;;;;;",
        "Boletos Recebidos;;;;;;;;;;",
        cab,
    ] + linhas_boleto
    p = tmp_path / "boletos.csv"
    p.write_text("\n".join(linhas) + "\n", encoding="latin-1")
    return p


def _linha(cnpj, venc, doc, valor, nome, barra=BARRA):
    return (f"ALFAMETAL S/A;{nome};{cnpj};{venc};{doc};{valor};{valor};"
            f"DMI;341;obs;{barra}")


def test_monta_e_duplica(tmp_path):
    molde = _molde_ret(tmp_path)
    csv = _csv(tmp_path, [
        _linha("90.100.200/0001-09", "24/09/2026", "0417529701", "84176,26",
               "USINA SIDERURGICA MG USIMINAS"),
    ])
    r = imp.processar(csv, CFG_RET, FILIAIS, ret_molde_path=molde,
                      ordenar_venc=True, duplicar_limite=0, duplicar_filial=True)

    assert r.total_lidos == 1
    assert r.total_validos == 1
    assert r.titulos_duplicados == 1
    gs = [l for l in r.linhas_finais if l[7:8] == "3" and l[13:14] == "G"]
    assert len(gs) == 3                       # matriz + 2 filiais

    cnpjs_ok = {"90100200000109"} | set(FILIAIS["90100200"])
    for g in gs:
        assert g[17:61] == BARRA                       # código de barras
        assert g[63:77] in cnpjs_ok                    # matriz ou filial
        assert g[77:107].strip() == "USINA SIDERURGICA MG USIMINAS"
        assert g[107:115] == "24092026"                # vencimento
        assert g[115:130] == f"{8417626:015d}"         # valor em centavos
        assert g[147:162].strip() == "4175297"          # NF tratada (sem 0/parcela)
    # aparecem a matriz E as filiais
    assert {g[63:77] for g in gs} == cnpjs_ok
    # trailers no fim, contagem coerente
    assert r.linhas_finais[-1].startswith("23799999")
    assert r.linhas_finais[-2][17:23] == f"{r.total_registros + 1:06d}"


def test_ignora_linha_invalida(tmp_path):
    molde = _molde_ret(tmp_path)
    csv = _csv(tmp_path, [
        _linha("90.100.200/0001-09", "24/09/2026", "0417529701", "84176,26",
               "USINA SIDERURGICA MG USIMINAS"),
        # barra curta -> nem conta como boleto
        "ALFAMETAL;X;90.100.200/0001-09;24/09/2026;9;10,00;10,00;DMI;341;o;123",
        # sem vencimento -> lido mas inválido
        _linha("90.100.200/0001-09", "", "0417529799", "50,00",
               "USINA SIDERURGICA MG USIMINAS"),
    ])
    r = imp.processar(csv, CFG_RET, FILIAIS, ret_molde_path=molde)
    assert r.total_lidos == 2          # a de barra curta não conta
    assert r.total_validos == 1        # a sem vencimento é descartada
    assert any("vencimento" in a.lower() for a in r.avisos)


def test_sem_cadastro_nao_duplica_mas_gera(tmp_path):
    molde = _molde_ret(tmp_path)
    csv = _csv(tmp_path, [
        _linha("99.999.999/0001-99", "24/09/2026", "0417529701", "84176,26",
               "FORNECEDOR X"),
    ])
    r = imp.processar(csv, CFG_RET, FILIAIS, ret_molde_path=molde)  # raiz fora do cadastro
    assert r.total_validos == 1
    assert r.titulos_duplicados == 0
    gs = [l for l in r.linhas_finais if l[7:8] == "3" and l[13:14] == "G"]
    assert len(gs) == 1                # passa intacto, 1 cópia
    assert gs[0][63:77] == "99999999000199"


def test_tratar_nota_corta_parcela_e_zero(tmp_path=None):
    # caso real: 0146463201 = 0 + NF(1464632) + parcela(01)  ->  1464632
    assert imp._tratar_nota("0146463201", {}) == "1464632"
    assert imp._tratar_nota("0417529701", {}) == "4175297"
    # sempre corta 2 (parcela do boleto), mesmo terminando em '001'
    assert imp._tratar_nota("0146516001", {}) == "1465160"
    # sem dígitos -> vazio
    assert imp._tratar_nota("", {}) == ""


def test_molde_padrao_alinhado(tmp_path=None):
    # o molde embutido tem que estar alinhado (valor da barra == campo de valor)
    assert imp._validar_molde(imp.DEFAULT_MOLDE_G)
    assert len(imp.DEFAULT_MOLDE_G) == 240
    assert imp.DEFAULT_MOLDE_G[7] == "3" and imp.DEFAULT_MOLDE_G[13] == "G"


def test_monta_com_molde_embutido(tmp_path):
    # sem RET: usa o molde padrão embutido — só precisa da planilha
    csv = _csv(tmp_path, [
        _linha("90.100.200/0001-09", "24/09/2026", "0417529701", "84176,26",
               "USINA SIDERURGICA MG USIMINAS"),
    ])
    # duplicação por filial é ON por padrão desde a v2.9; aqui desligamos
    # de propósito para conferir o registro montado a partir do molde
    r = imp.processar(csv, CFG_RET, FILIAIS, duplicar_filial=False)
    assert r.total_validos == 1
    assert r.titulos_duplicados == 0
    gs = [l for l in r.linhas_finais if l[7:8] == "3" and l[13:14] == "G"]
    assert len(gs) == 1                          # 1 boleto -> 1 linha (sem duplicar)
    g = gs[0]
    assert g[17:61] == BARRA
    assert g[107:115] == "24092026"
    assert g[115:130] == f"{8417626:015d}"
    assert g[147:162].strip() == "4175297"
    assert g[63:77] == "90100200000109"          # CNPJ da matriz, sem duplicar


def test_padrao_duplica_por_filial(tmp_path):
    # sem passar nada, cada boleto de usina vira cópias com CNPJ de filial
    csv = _csv(tmp_path, [
        _linha("90.100.200/0001-09", "24/09/2026", "0417529701", "84176,26",
               "USINA SIDERURGICA MG USIMINAS"),
    ])
    r = imp.processar(csv, CFG_RET, FILIAIS)
    assert r.total_validos == 1
    assert r.titulos_duplicados >= 1
    gs = [l for l in r.linhas_finais if l[7:8] == "3" and l[13:14] == "G"]
    assert {g[63:77] for g in gs} <= {"90100200000109"} | set(FILIAIS["90100200"])


def test_relatorio_colisoes(tmp_path):
    # duas notas com o MESMO valor -> o sistema não distingue -> colisão
    csv = _csv(tmp_path, [
        _linha("90.100.200/0001-09", "24/09/2026", "0146516001", "94807,76",
               "USINA SIDERURGICA MG USIMINAS", barra=BARRA),
        _linha("90.100.200/0001-09", "24/09/2026", "0146463201", "94807,76",
               "USINA SIDERURGICA MG USIMINAS",
               barra="34197157900094807761090000000000000000000002"),
    ])
    r = imp.processar(csv, CFG_RET, FILIAIS)
    assert r.total_validos == 2
    assert r.total_colisoes == 2                  # as duas colidem no valor
    assert len(r.colisoes) == 1                   # um grupo de valor repetido
    assert "1465160" in r.colisoes[0] and "1464632" in r.colisoes[0]


def _run_all():
    import tempfile
    import traceback
    falhas = 0
    for nome, fn in sorted(globals().items()):
        if nome.startswith("test_") and callable(fn):
            try:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
                print(f"OK  {nome}")
            except Exception:
                falhas += 1
                print(f"FALHOU  {nome}")
                traceback.print_exc()
    print("\n" + ("TODOS OS TESTES PASSARAM." if not falhas
                  else f"{falhas} teste(s) falharam."))
    return falhas


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
