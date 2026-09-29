"""Classificador da auditoria semântica de churn — regras puras, sem banco.

As frases são as formas que aparecem nas observações de cancelamento do IXC
(sem nome de cliente). O que se protege aqui é o que a auditoria manual de
set/2026 ensinou: o motivo escolhido no menu mente com frequência, e a
observação decide.
"""

from __future__ import annotations

from datetime import date

import pytest

from apps.analytics.domain.churn_audit import (
    CAUSAS_CONHECIDAS,
    CONC_COMBO,
    CONC_INSTALADA,
    CONC_PRECO,
    CONC_SEM_MOTIVO,
    DESISTENCIA,
    FALECIMENTO,
    IMOVEL_DESOCUPADO,
    INADIMPLENCIA,
    MUD_CONCORRENTE,
    MUD_DENTRO_COBERTURA,
    MUD_INTERNET_EXISTENTE,
    MUD_NAO_INFORMADO,
    MUD_NAO_VERIFICADA,
    MUD_SEM_COBERTURA,
    NIVEIS,
    REGRA_BLOQUEIO_AUTO,
    REGRA_BLOQUEIO_MANUAL,
    REGRA_CANCELAMENTO,
    SISTEMICO,
    TEC_LENTIDAO,
    TEC_NAO_DETALHADO,
    TEC_QUEDAS,
    TEC_TERCEIRO,
    TEC_VISITA,
    TEC_WIFI,
    TITULARIDADE,
    VOL_FINANCEIRA,
    VOL_PERDA,
    VOL_PRODUTO,
    VOL_SEM_MOTIVO,
    MotivoTipo,
    Nivel,
    classificar,
    competencia,
    motivo_tipo,
    normalizar,
)


class TestMotivoTipo:
    @pytest.mark.parametrize(
        ("nome", "tipo"),
        [
            ("Inadimplência", MotivoTipo.INADIMPLENCIA),
            ("inadimplência", MotivoTipo.INADIMPLENCIA),
            ("Mudou de operadora", MotivoTipo.OPERADORA),
            ("Desconexão por opção", MotivoTipo.OPCAO),
            ("Mudou de endereço", MotivoTipo.ENDERECO),
            ("Endereço não cabeado", MotivoTipo.NAO_CABEADO),
            ("Mudou de cidade", MotivoTipo.CIDADE),
            ("Problemas técnicos", MotivoTipo.TECNICO),
            ("Troca de titularidade", MotivoTipo.TITULARIDADE),
            ("Falecimento", MotivoTipo.FALECIMENTO),
            ("Desistência da assinatura", MotivoTipo.DESISTENCIA),
            # "não informado" no nome não pode virar "sem motivo".
            ("Desistência da assinatura (não informado)", MotivoTipo.DESISTENCIA),
            ("Acerto sistêmico", MotivoTipo.SISTEMICO),
            ("Não houve navegação", MotivoTipo.SISTEMICO),
            ("Teste (Datacake)", MotivoTipo.SISTEMICO),
            ("Contrato vindo cancelado do SGP", MotivoTipo.IMPORTADO),
            ("Sem motivo registrado", MotivoTipo.NENHUM),
            ("Motivo #12", MotivoTipo.NENHUM),
        ],
    )
    def test_nome_do_ixc_vira_tipo(self, nome: str, tipo: MotivoTipo) -> None:
        assert motivo_tipo(nome) is tipo


class TestMotivoFechado:
    """Motivos que a observação não muda."""

    def test_inadimplencia_ignora_a_observacao(self) -> None:
        v = classificar("Inadimplência", "cliente se mudou para onde não temos cobertura")
        assert v.causa_raiz == INADIMPLENCIA
        assert v.nivel is Nivel.PARCIAL
        assert not v.inconsistente

    def test_titularidade_fica_fora_do_churn(self) -> None:
        v = classificar("Troca de titularidade", "titularidade")
        assert v.causa_raiz == TITULARIDADE
        assert v.nivel is Nivel.FORA

    def test_contrato_importado_do_sgp_nunca_e_perda_desta_base(self) -> None:
        v = classificar("Contrato vindo cancelado do SGP", "cliente trocou de operadora")
        assert v.causa_raiz == SISTEMICO
        assert v.nivel is Nivel.FORA

    def test_mudou_de_cidade_e_sem_cobertura_por_padrao(self) -> None:
        v = classificar("Mudou de cidade", "cliente pediu o cancelamento")
        assert v.causa_raiz == MUD_SEM_COBERTURA
        assert v.nivel is Nivel.INEVITAVEL


class TestDesconexaoPorOpcao:
    """O motivo mais genérico — e o que mais esconde outra história."""

    def test_sem_sinal_e_perda_de_necessidade(self) -> None:
        v = classificar("Desconexão por opção", "cancelou por motivos pessoais")
        assert v.causa_raiz == VOL_PERDA
        assert v.nivel is Nivel.PARCIAL
        assert not v.inconsistente

    def test_recusa_de_motivo_exige_sondagem(self) -> None:
        v = classificar("Desconexão por opção", "Cliente solicitou o cancelamento, mas não quis informar o motivo")
        assert v.causa_raiz == VOL_SEM_MOTIVO
        assert v.nivel is Nivel.PODERIA

    def test_mudanca_sem_cobertura_e_inconsistente(self) -> None:
        v = classificar("Desconexão por opção", "cliente se mudou para onde não temos cobertura")
        assert v.causa_raiz == MUD_SEM_COBERTURA
        assert v.nivel is Nivel.INEVITAVEL
        assert v.inconsistente

    def test_queixa_tecnica_vence_o_motivo(self) -> None:
        v = classificar("Desconexão por opção", "alegou que tinha problemas ao usar nossa conexão")
        assert v.causa_raiz == TEC_NAO_DETALHADO
        assert v.nivel is Nivel.PODERIA
        assert v.inconsistente

    def test_iptv_e_servico_de_terceiro(self) -> None:
        v = classificar("Desconexão por opção", "disse que a internet trava muito no aparelho box (IPTV)")
        assert v.causa_raiz == TEC_TERCEIRO

    def test_combo_movel(self) -> None:
        v = classificar("Desconexão por opção", "trocou para uma operadora com dados moveis")
        assert v.causa_raiz == CONC_COMBO

    def test_desemprego_e_restricao_financeira(self) -> None:
        v = classificar("Desconexão por opção", "ficou desempregado e prefere reduzir os custos")
        assert v.causa_raiz == VOL_FINANCEIRA
        assert v.nivel is Nivel.PODERIA

    def test_cargo_financeiro_nao_e_restricao_financeira(self) -> None:
        """"Sócia e financeiro da empresa" é quem ligou, não o motivo."""
        v = classificar(
            "Desconexão por opção",
            "a sócia e financeiro da empresa pediu o cancelamento pois as empresas fecharam",
        )
        assert v.causa_raiz != VOL_FINANCEIRA

    def test_pergunta_do_atendente_nao_e_proposta(self) -> None:
        v = classificar(
            "Desconexão por opção",
            "disse que não vai mais precisar, tentei verificar com ele se ele recebeu uma "
            "proposta melhor, mas só queria cancelar",
        )
        assert v.causa_raiz == VOL_PERDA

    def test_so_a_tv(self) -> None:
        v = classificar("Desconexão por opção", "possui apenas a televisão e não quer mais")
        assert v.causa_raiz == VOL_PRODUTO

    def test_titularidade_escrita_na_observacao(self) -> None:
        v = classificar("Desconexão por opção", "troca de titularidade")
        assert v.causa_raiz == TITULARIDADE
        assert v.nivel is Nivel.FORA

    def test_falecimento_escrito_na_observacao(self) -> None:
        v = classificar("Desconexão por opção", "falecimento de quem utilizava")
        assert v.causa_raiz == FALECIMENTO


class TestMudouDeOperadora:
    def test_so_a_troca(self) -> None:
        v = classificar("Mudou de operadora", "cliente trocou de operadora")
        assert v.causa_raiz == CONC_SEM_MOTIVO
        assert v.nivel is Nivel.PODERIA

    def test_mudou_de_operadora_nao_e_mudanca_de_endereco(self) -> None:
        v = classificar("Mudou de operadora", "Mudou para a claro")
        assert v.causa_raiz == CONC_SEM_MOTIVO

    def test_combo(self) -> None:
        v = classificar("Mudou de operadora", "contratou um plano com celular + residencial")
        assert v.causa_raiz == CONC_COMBO

    def test_proposta(self) -> None:
        v = classificar("Mudou de operadora", "recebeu uma proposta melhor de outra operadora")
        assert v.causa_raiz == CONC_PRECO

    def test_ja_instalada(self) -> None:
        v = classificar("Mudou de operadora", "trocou de operadora e já instalaram a nova internet")
        assert v.causa_raiz == CONC_INSTALADA

    def test_mudanca_com_concorrente_e_inconsistente(self) -> None:
        v = classificar("Mudou de operadora", "se mudou de casa e no novo endereço já contratou outra internet")
        assert v.causa_raiz == MUD_CONCORRENTE
        assert v.inconsistente

    def test_falha_tecnica_antes_da_troca(self) -> None:
        v = classificar("Mudou de operadora", "trocou de operadora, a internet não funcionava bem na casa toda")
        assert v.causa_raiz == TEC_WIFI
        assert v.destino_final == "Troca para concorrente após falha técnica"
        assert v.inconsistente

    def test_pergunta_sobre_troca_nao_e_troca(self) -> None:
        """"Não falou se foi troca de operadora" não afirma troca nenhuma."""
        v = classificar(
            "Desistência da assinatura",
            "só alegou não querer mais. Não falou se foi troca de operadora",
        )
        assert v.familia.value != "concorrencia"


class TestMudancaDeEndereco:
    def test_sem_evidencia_e_cobertura_nao_verificada(self) -> None:
        v = classificar("Mudou de endereço", "mudou de endereço")
        assert v.causa_raiz == MUD_NAO_VERIFICADA
        assert v.nivel is Nivel.PODERIA

    def test_lugar_fora_da_area(self) -> None:
        v = classificar("Mudou de endereço", "está se mudando para a Bahia")
        assert v.causa_raiz == MUD_SEM_COBERTURA

    def test_internet_ja_existente(self) -> None:
        v = classificar("Mudou de endereço", "se mudou e o novo endereço já possui internet inclusa")
        assert v.causa_raiz == MUD_INTERNET_EXISTENTE
        assert v.nivel is Nivel.PARCIAL

    def test_endereco_nao_informado(self) -> None:
        v = classificar("Mudou de endereço", "se mudou de casa, porém não quis me informar o novo endereço")
        assert v.causa_raiz == MUD_NAO_INFORMADO

    def test_dentro_da_cobertura_era_transferencia(self) -> None:
        """A verificação foi feita e deu positivo: a transferência era possível."""
        v = classificar(
            "Desistência da assinatura",
            "mudou para outra cidade, porém está dentro da área de cobertura",
        )
        assert v.causa_raiz == MUD_DENTRO_COBERTURA
        assert v.nivel is Nivel.PODERIA

    def test_negacao_desfaz_a_mudanca(self) -> None:
        v = classificar(
            "Desconexão por opção",
            "entregou o ponto, não vai ficar ninguém no local e por enquanto ele não irá se mudar",
        )
        assert v.causa_raiz == IMOVEL_DESOCUPADO


class TestProblemasTecnicos:
    def test_sem_detalhe(self) -> None:
        v = classificar("Problemas técnicos", "teve problemas com a nossa internet")
        assert v.causa_raiz == TEC_NAO_DETALHADO
        assert v.nivel is Nivel.PODERIA
        assert v.subtipo_tecnico == "Outros/não detalhado"

    def test_lentidao(self) -> None:
        v = classificar("Problemas técnicos", "cancelamento por conta de lentidão")
        assert v.causa_raiz == TEC_LENTIDAO

    def test_reincidencia_sobe_para_deveria(self) -> None:
        v = classificar("Problemas técnicos", "disse que sempre teve problemas com a internet")
        assert v.nivel is Nivel.DEVERIA
        assert v.subtipo_tecnico == "Reincidência"

    def test_reincidencia_com_subtipo(self) -> None:
        v = classificar(
            "Problemas técnicos",
            "a internet não é boa, precisa chamar o técnico várias vezes e mesmo assim sempre cai",
        )
        assert v.causa_raiz == TEC_QUEDAS
        assert v.nivel is Nivel.DEVERIA
        assert v.subtipo_tecnico == "Quedas/oscilação + reincidência"

    def test_sem_reclamacao_ha_tempo_nao_e_reincidencia(self) -> None:
        """O falso positivo da auditoria original: a observação diz o contrário."""
        v = classificar(
            "Problemas técnicos",
            "alegou problemas com a conexão, porém olhei os históricos e o cliente não "
            "entrava em contato a muito tempo reclamando de conexão",
        )
        assert v.nivel is Nivel.PODERIA

    def test_visita_nao_enviada(self) -> None:
        v = classificar(
            "Problemas técnicos",
            "entrou em contato várias vezes e não enviamos um técnico no local",
        )
        assert v.causa_raiz == TEC_VISITA
        assert v.nivel is Nivel.DEVERIA

    def test_nao_quis_aguardar_a_manutencao(self) -> None:
        """A visita carrega a negação no próprio padrão — não pode ser anulada."""
        v = classificar("Desistência da assinatura", "cliente não quer aguardar a manutenção e deseja cancelar")
        assert v.causa_raiz == TEC_VISITA

    def test_troca_depois_da_falha_muda_o_destino(self) -> None:
        v = classificar("Problemas técnicos", "teve muitos problemas com a internet e já trocou de operadora")
        assert v.destino_final == "Troca para concorrente após falha técnica"
        assert v.subtipo_concorrencia == "Problema técnico anterior"
        assert v.justificativa.endswith("e posterior troca de operadora.")

    def test_negacao_de_problema_tecnico(self) -> None:
        v = classificar(
            "Mudou de operadora",
            "contratou outra operadora, verifiquei e não teve nenhum atendimento de problema técnico",
        )
        assert v.familia.value == "concorrencia"


class TestSistemicoEDesistencia:
    def test_acerto_sistemico(self) -> None:
        v = classificar("Acerto sistêmico", "criei um contrato errado")
        assert v.causa_raiz == SISTEMICO
        assert v.nivel is Nivel.FORA

    def test_nao_houve_navegacao_com_historia_de_cliente(self) -> None:
        """Motivo administrativo, mas a observação conta uma perda real."""
        v = classificar("Não houve navegação", "solicitou o cancelamento por corte de custos")
        assert v.causa_raiz == VOL_FINANCEIRA
        assert v.inconsistente

    def test_desistencia_sem_explicacao(self) -> None:
        v = classificar("Desistência da assinatura", "Sem motivo")
        assert v.causa_raiz == DESISTENCIA
        assert v.nivel is Nivel.PODERIA

    def test_desistencia_que_era_titularidade(self) -> None:
        v = classificar("Desistência da assinatura", "mudança de titularidade")
        assert v.causa_raiz == TITULARIDADE
        assert v.inconsistente

    def test_sem_motivo_cadastrado_nunca_e_inconsistente(self) -> None:
        v = classificar("Sem motivo registrado", "se mudou para onde não temos cobertura")
        assert v.causa_raiz == MUD_SEM_COBERTURA
        assert not v.inconsistente


class TestCompetencia:
    CANCELADO = date(2026, 6, 20)

    def test_inadimplencia_conta_do_bloqueio_mais_recente(self) -> None:
        c = competencia(
            INADIMPLENCIA,
            self.CANCELADO,
            bloqueio_auto=date(2026, 3, 1),
            bloqueio_manual=date(2026, 4, 10),
        )
        assert c.data == date(2026, 5, 10)
        assert c.base == date(2026, 4, 10)
        assert c.regra == REGRA_BLOQUEIO_MANUAL

    def test_bloqueio_automatico(self) -> None:
        c = competencia(INADIMPLENCIA, self.CANCELADO, bloqueio_auto=date(2026, 5, 2), bloqueio_manual=None)
        assert c.data == date(2026, 6, 1)
        assert c.regra == REGRA_BLOQUEIO_AUTO

    def test_inadimplencia_sem_bloqueio_usa_o_cancelamento(self) -> None:
        c = competencia(INADIMPLENCIA, self.CANCELADO, bloqueio_auto=None, bloqueio_manual=None)
        assert c.data == self.CANCELADO
        assert c.regra == REGRA_CANCELAMENTO

    def test_outras_causas_ignoram_o_bloqueio(self) -> None:
        c = competencia(
            MUD_SEM_COBERTURA, self.CANCELADO, bloqueio_auto=date(2026, 1, 1), bloqueio_manual=None
        )
        assert c.data == self.CANCELADO
        assert c.regra == REGRA_CANCELAMENTO


class TestCatalogo:
    def test_toda_causa_devolvida_esta_no_catalogo(self) -> None:
        frases = [
            ("Problemas técnicos", "lentidão"),
            ("Problemas técnicos", "quedas"),
            ("Problemas técnicos", "roteador com defeito"),
            ("Problemas técnicos", "wi-fi fraco"),
            ("Problemas técnicos", "iptv"),
            ("Problemas técnicos", "não enviamos um técnico"),
            ("Problemas técnicos", ""),
            ("Desconexão por opção", ""),
            ("Mudou de operadora", ""),
            ("Mudou de endereço", ""),
            ("Sem motivo registrado", ""),
        ]
        for motivo, obs in frases:
            assert classificar(motivo, obs).causa_raiz in CAUSAS_CONHECIDAS

    def test_niveis_em_ordem_de_prioridade(self) -> None:
        assert [n.nivel for n in NIVEIS] == [
            Nivel.DEVERIA, Nivel.PODERIA, Nivel.PARCIAL, Nivel.INEVITAVEL, Nivel.FORA,
        ]

    def test_normalizar(self) -> None:
        assert normalizar("  Mudança   de\nENDEREÇO ") == "mudanca de endereco"
