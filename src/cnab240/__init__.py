"""cnab240 — juntar, validar e montar arquivos bancários CNAB240.

Três módulos, só com a biblioteca padrão (openpyxl é opcional, para .xlsx):

- ``cnab240.ret``: junta vários .RET de cobrança (segmentos G/H/Y/J) num
  arquivo de importação, converte Itaú → Bradesco e recalcula os trailers.
- ``cnab240.retorno``: combina retornos de pagamento SISPAG (segmentos
  A/B/J, vários lotes) num arquivo só, renumerando os lotes.
- ``cnab240.importacao_usinas``: monta registros segmento G a partir de uma
  planilha de boletos (DDA), tirando o vencimento do código de barras.
"""

from . import importacao_usinas, ret, retorno

__all__ = ["ret", "retorno", "importacao_usinas"]
__version__ = "0.1.0"
