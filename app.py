# tabela diária (mais recente -> mais antigo)
mes_df = mes_df.sort_values("Data", ascending=False)

show = mes_df[["Data", "Cadastradas", "Abertas", "% Abertas/Cadastradas", "Indicador", "Percentual_num"]].copy()
show["Data"] = show["Data"].apply(fmt_date)

# formata milhares com ponto
show["Cadastradas"] = show["Cadastradas"].apply(br_int)
show["Abertas"] = show["Abertas"].apply(br_int)

# remove a coluna técnica antes de mostrar
show_view = show.drop(columns=["Percentual_num"])

st.caption("Tabela diária do mês (azul ≥ 20% | vermelho < 20%)")
st.dataframe(
    style_conversao_table(show).hide(axis="index"),  # usa Percentual_num para cor
    use_container_width=True,
    hide_index=True
)

# ✅ resumo final do mês (somente do mês selecionado)
st.markdown("#### Fechamento do mês (somatório e percentual geral)")
total_ab = int(mes_df["Abertas"].sum())
total_cad = int(mes_df["Cadastradas"].sum())
perc_mes = (total_ab / total_cad) if total_cad > 0 else 0.0

f1, f2, f3 = st.columns(3)
f1.metric("Total Cadastradas (mês)", br_int(total_cad))
f2.metric("Total Abertas (mês)", br_int(total_ab))
f3.metric("% Geral (mês)", f"{perc_mes*100:.1f}%".replace(".", ","))
