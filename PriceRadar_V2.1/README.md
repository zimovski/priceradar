# PriceRadar V2.1

## Hospedagem de teste (Render)

Esta versão adiciona uma rota pública de callback para o OAuth do Mercado Livre e um `render.yaml` para facilitar o deploy. O nome sugerido do serviço é `priceradar`. Se estiver disponível, a URL será `https://priceradar.onrender.com` e a URI de redirect será `https://priceradar.onrender.com/mercadolivre/callback`.

> Importante: o plano gratuito do Render usa disco efêmero. Portanto o SQLite desta V2.1 é apenas para testes de hospedagem/OAuth. Antes de começar a guardar histórico real na nuvem, conectaremos um PostgreSQL persistente.

Protótipo funcional de comparador/rastreador de preços pensado para continuar sendo o mesmo produto quando migrarmos para Web hospedado e Android.

## O que mudou na V2

- Busca por texto continua sendo a porta de entrada.
- Primeiro conector real: Mercado Livre.
- A busca combina produtos já acompanhados com produtos reais retornados pela API do Mercado Livre.
- Ao clicar em **Acompanhar** em um produto real, o PriceRadar cria o produto canônico, liga-o ao ID de catálogo do Mercado Livre e registra o preço atual como primeira observação.
- Botão **Atualizar preço agora** registra uma nova observação.
- Enquanto o backend estiver aberto, existe uma coleta automática a cada 6 horas por padrão.
- Access Token / Refresh Token / APP ID / Secret Key podem ser guardados pelo próprio app no Gerenciador de Credenciais do Windows.
- O banco segue preparado para histórico externo (`external`) de Buscapé/Zoom e para novos conectores (KaBuM, Magalu, Casas Bahia).
- O frontend Flutter foi atualizado para usar o mesmo contrato de busca da V2.

## Instalação no Windows

1. Extraia a pasta do projeto.
2. Abra `backend`.
3. Execute `instalar_windows.bat` uma vez.
4. Execute `abrir_app_windows.bat`.
5. O navegador abrirá em `http://127.0.0.1:8000`.

O banco começa vazio. Nenhum PS5 ou outro produto é cadastrado automaticamente.

## Por que a busca real precisa de credenciais do Mercado Livre?

A documentação atual do Mercado Livre mostra `Authorization: Bearer $ACCESS_TOKEN` no endpoint de busca de produtos `/products/search`. Portanto a V2 não finge dados e não faz scraping do site: ela só mostra resultados reais quando a API oficial estiver configurada.

Documentação oficial:
- https://developers.mercadolivre.com.br/buscador-de-produtos
- https://developers.mercadolivre.com.br/pt_br/autenticacao-e-autorizacao
- https://developers.mercadolivre.com.br/pt_br/crie-uma-aplicacao-no-mercado-livre

## Configuração do Mercado Livre

Na tela inicial, abra **Configurar Mercado Livre**.

Campos:
- `Access Token`: obrigatório para começar.
- `Refresh Token`: recomendado.
- `APP ID`: recomendado junto do Refresh Token.
- `Secret Key`: recomendado junto do Refresh Token.

Com os quatro dados, se o Access Token expirar, o backend tenta renová-lo automaticamente usando o Refresh Token. Tokens renovados também são guardados no Gerenciador de Credenciais do Windows.

> Não compartilhe seu Access Token, Refresh Token ou Secret Key. Eles não devem ser enviados junto com o ZIP para amigos. Na futura versão hospedada, essas credenciais ficarão somente no servidor do PriceRadar e os usuários não precisarão vê-las.

## Fluxo atual

```text
usuário digita um produto
        ↓
PriceRadar busca no banco local
        +
Mercado Livre /products/search
        ↓
resultados reais de catálogo
        ↓
usuário escolhe o produto correto
        ↓
PriceRadar salva o vínculo externo
        ↓
registra preço atual do buy box winner
        ↓
histórico próprio
```

O produto de catálogo é usado para reduzir o risco de misturar versões diferentes do mesmo item. O detalhe `/products/{product_id}` disponibiliza o `buy_box_winner`, que usamos como oferta atual do Mercado Livre.

## Coleta automática

Enquanto `abrir_app_windows.bat` estiver rodando, o backend tenta atualizar produtos acompanhados a cada 6 horas.

Para trocar o intervalo, defina antes de iniciar:

```bat
set COLLECTION_INTERVAL_HOURS=12
```

Para desativar:

```bat
set AUTO_COLLECTION=0
```

Quando levarmos o backend para a nuvem, a coleta continuará mesmo com o computador do usuário desligado.

## Histórico Buscapé / Zoom

A estrutura de importação continua disponível em:

`POST /api/products/{product_id}/history/import`

Essas observações entram com `source_kind = external`, separadas das observações feitas pelo próprio PriceRadar. A V2 ainda não automatiza a leitura do histórico do Buscapé/Zoom porque não encontramos uma API pública oficial de leitura dessas séries históricas. Isso será tratado como integração opcional, sem tornar o app dependente dela.

## Próximos conectores planejados

1. Mercado Livre (V2 atual)
2. KaBuM
3. Magazine Luiza
4. Casas Bahia
5. Importadores/histórico externo Buscapé e Zoom, quando houver método autorizado e estável

## Estrutura

```text
backend/
  app/
    main.py
    models.py
    providers/
      mercadolivre.py
      buscape.py
      zoom.py
    services/
      collector.py
      analytics.py
    static/
      index.html
frontend/
  lib/
    main.dart
    api.dart
    models.dart
```

## Observação para produção

Esta versão é um protótipo local. Antes de disponibilizar para várias pessoas, o próximo passo estrutural será hospedar FastAPI + PostgreSQL em um servidor e manter credenciais de lojas somente no servidor. O Flutter poderá então apontar para a mesma API tanto na Web quanto no Android.
