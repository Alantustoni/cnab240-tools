"""Demo: junta dois .RET sintéticos (Bradesco + Itaú) num arquivo só.

    python examples/demo.py

Os arquivos são gerados na hora, com dados fictícios, numa pasta temporária.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cnab240 import ret  # noqa: E402


def segmento_g(banco, seq, nome, venc, valor_centavos, nota):
    """Um título (segmento G) no leiaute de 240 posições."""
    linha = (f"{banco}0001" + "3" + f"{seq:05d}" + "G" + " " + "01"
             + "0" * 44                      # código de barras   [17:61]
             + "2" + "0" * 15                # inscrição + CNPJ   [61:77]
             + nome.ljust(30)[:30]           # favorecido         [77:107]
             + venc                          # vencimento         [107:115]
             + f"{valor_centavos:015d}"      # valor              [115:130]
             + "0" * 15 + "09"
             + nota.ljust(15)[:15])          # nº da nota         [147:162]
    return linha.ljust(240)


def arquivo_ret(pasta, nome, banco, titulos):
    linhas = [f"{banco}00000".ljust(240), f"{banco}00011".ljust(240)]
    for i, (fav, venc, valor, nota) in enumerate(titulos, 1):
        linhas.append(segmento_g(banco, i, fav, venc, valor, nota))
    linhas += [f"{banco}00015".ljust(240), f"{banco}99999".ljust(240)]
    p = Path(pasta) / nome
    p.write_text("\r\n".join(linhas) + "\r\n", encoding="latin-1")
    return p


with tempfile.TemporaryDirectory() as pasta:
    bradesco = arquivo_ret(pasta, "bradesco.ret", "237", [
        ("FORNECEDOR ALFA LTDA", "15112026", 125_000, "1001"),
        ("FORNECEDOR BETA SA", "05112026", 89_990, "1002"),
    ])
    itau = arquivo_ret(pasta, "itau.ret", "341", [
        ("FORNECEDOR GAMA ME", "10112026", 45_050, "2001"),
    ])

    cfg = {"filtro_estrutural": True, "recalcular_trailer": True,
           "ordenar_por_vencimento": True, "excluir_usinas": False,
           "separar_por_empresa": False}
    r = ret.processar([bradesco, itau], cfg)

    print(f"arquivos: {r.total_arquivos} | títulos: {r.total_titulos} "
          f"| convertidos do Itaú: {r.convertidos_itau}")
    print(f"vencimentos: {r.primeiro_vencimento} a {r.ultimo_vencimento}")
    valor_br = f"{r.valor_titulos:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    print(f"valor total: R$ {valor_br}")
    print("\nprimeiras colunas de cada linha do arquivo final:")
    for linha in r.linhas_finais:
        print("  " + linha[:30].rstrip())

    saida = Path(pasta) / "importacao.txt"
    ret.gravar(r, saida)
    print(f"\ngravado: {saida.name} ({len(saida.read_bytes())} bytes, CRLF, latin-1)")
