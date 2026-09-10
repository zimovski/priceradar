# PriceRadar V2.4

PriceRadar é um comparador e rastreador de preços em desenvolvimento. A arquitetura mantém interface, API, integrações e histórico desacoplados para facilitar a futura migração/expansão para Flutter e Android.

## O que já funciona

- Busca por texto no catálogo real do Mercado Livre.
- OAuth do Mercado Livre com PKCE e renovação por refresh token.
- Identificação do produto de catálogo e melhor oferta exposta pela API.
- Registro de observações de preço em PostgreSQL.
- Histórico de até 2 anos na interface.
- Dashboard de produtos acompanhados.
- Menor preço atual, mínimo/máximo histórico e média de 90 dias.
- Índice experimental de momento de compra conforme o histórico cresce.
- Histórico diário e tabela de observações.
- Interface responsiva para desktop e celular.
- Cache/ranking da busca para reduzir resultados irrelevantes e chamadas de API.
- Tokens do Mercado Livre cifrados no banco usando segredo mantido no servidor.
- Limites básicos de requisição e bloqueio de endpoints administrativos antigos.

## Produção de teste

Backend atual: `https://priceradar-luwn.onrender.com`

URI OAuth cadastrada no Mercado Livre:

`https://priceradar-luwn.onrender.com/mercadolivre/oauth/callback`

Start Command no Render:

`uvicorn app.oauth_wrapper:app --host 0.0.0.0 --port $PORT`

Root Directory:

`PriceRadar_V2.1/backend`

Variáveis já esperadas no Render:

- `DATABASE_URL`
- `MERCADOLIVRE_APP_ID`
- `MERCADOLIVRE_CLIENT_SECRET`
- `AUTO_COLLECTION=0`

## Coleta automática

Existe um workflow em `.github/workflows/price-collection.yml` preparado para chamar a coleta a cada 6 horas. Para ativá-lo com segurança, configure o mesmo segredo em dois locais:

- Render: `COLLECTOR_SECRET`
- GitHub Actions Secret: `PRICERADAR_COLLECTOR_SECRET`

Enquanto esses segredos não existirem, a coleta agendada não executa e o endpoint global permanece protegido.

## Fontes futuras

A estrutura prevê conectores independentes para KaBuM, Magazine Luiza, Casas Bahia e fontes de histórico externo. Dados externos devem permanecer identificados separadamente dos preços observados pelo próprio PriceRadar.

## Próximas prioridades

1. Ativar e validar coleta automática.
2. Adicionar KaBuM como segunda fonte real.
3. Adicionar Magalu e Casas Bahia.
4. Implementar contas individuais/watchlists antes de liberar mutações sensíveis para terceiros.
5. Investigar importação permitida de histórico externo (Buscapé/Zoom e outras fontes).
6. Evoluir o frontend Flutter para Android mantendo esta API como backend central.
