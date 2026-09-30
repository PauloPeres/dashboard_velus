"""O padrão por PON — a assinatura do corte de tronco (massiva 359).

O rótulo aparece no topo da massiva e na mensagem do técnico, antes de qualquer
mapa. Rótulo errado aqui manda procurar cabo quando o problema é a OLT, ou o
contrário — por isso cada forma tem o seu teste, e a forma que não se sabe
classificar fica sem rótulo.
"""

from __future__ import annotations

from apps.network.domain.pon_pattern import (
    OLT_INTEIRA,
    PON_INTEIRA,
    TRONCO,
    classificar,
)


def _pons(n: int, *, inicio: int, fora: int, no_ar: int) -> tuple[dict[str, int], dict[str, int]]:
    ids = [str(inicio + i) for i in range(n)]
    return {p: fora for p in ids if fora}, {p: no_ar for p in ids if no_ar}


class TestCorteDeTronco:
    def test_a_massiva_359(self) -> None:
        """42 PONs inteiras, uma parcial, uma com ruído, o resto da OLT no ar."""
        fora, no_ar = _pons(42, inicio=100, fora=10, no_ar=0)
        fora["160"], no_ar["160"] = 2, 1  # 67%: parcial
        fora["223"], no_ar["223"] = 2, 19  # 10%: ruído, conta como no ar
        _, intactas = _pons(100, inicio=300, fora=0, no_ar=20)
        no_ar.update(intactas)

        padrao = classificar(fora, no_ar)
        assert padrao.tipo == TRONCO
        assert len(padrao.pons_inteiras) == 42
        assert padrao.pons_parciais == ("160",)
        assert padrao.pons_intactas == 101

    def test_pon_inteira_com_um_cliente_que_voltou_continua_inteira(self) -> None:
        """Sempre há quem volte antes: 9 de 10 é PON inteira, não parcial."""
        fora = {"1": 9, "2": 10}
        no_ar = {"1": 1, "3": 20}
        assert classificar(fora, no_ar).tipo == TRONCO


class TestOutrasFormas:
    def test_todas_as_pons_da_olt_fora_e_olt_inteira(self) -> None:
        """Antes de procurar cabo, olhe o equipamento e a energia do POP."""
        fora, no_ar = _pons(16, inicio=1, fora=12, no_ar=0)
        assert classificar(fora, no_ar).tipo == OLT_INTEIRA

    def test_uma_pon_so(self) -> None:
        fora = {"7": 30}
        no_ar = {"7": 1, "8": 25, "9": 40}
        padrao = classificar(fora, no_ar)
        assert padrao.tipo == PON_INTEIRA
        assert padrao.pons_inteiras == ("7",)

    def test_quedas_parciais_espalhadas_ficam_sem_rotulo(self) -> None:
        """Ramal depois do splitter, energia de bairro, dois eventos: não se sabe."""
        fora = {str(p): 5 for p in range(1, 6)}
        no_ar = {str(p): 10 for p in range(1, 6)}
        padrao = classificar(fora, no_ar)
        assert padrao.tipo == ""
        assert padrao.tem_padrao is False

    def test_pon_de_um_cliente_so_nao_conta(self) -> None:
        """1 de 1 fora é 100% e não significa nada."""
        fora = {"1": 1, "2": 1}
        no_ar = {"3": 20}
        assert classificar(fora, no_ar).tipo == ""

    def test_pons_em_ordem_numerica(self) -> None:
        fora = {"10": 5, "9": 5, "100": 5}
        assert classificar(fora, {"50": 9}).pons_inteiras == ("9", "10", "100")
