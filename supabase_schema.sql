-- ============================================================================
-- Execute este SQL uma única vez no SQL Editor do Supabase (plano gratuito)
-- para conectar o Bot na Discloud Gold ao Painel Web na Vercel!
-- ============================================================================

-- 1) Estado sincronizado do painel (produtos, histórico, Mini-SAD e agendamentos)
CREATE TABLE IF NOT EXISTS public.buscador_estado (
    id TEXT PRIMARY KEY DEFAULT 'principal',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2) Fila de comandos enviados pelo site na Vercel para o bot na Discloud executar
CREATE TABLE IF NOT EXISTS public.buscador_comandos (
    id BIGSERIAL PRIMARY KEY,
    acao TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pendente',
    resultado JSONB,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processado_em TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_buscador_comandos_status
    ON public.buscador_comandos (status, id);

-- Permissões para acesso via chave anon/publishable do projeto (Painel Web + Bot)
ALTER TABLE public.buscador_estado ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.buscador_comandos ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "allow_all_estado" ON public.buscador_estado;
CREATE POLICY "allow_all_estado"
    ON public.buscador_estado
    FOR ALL
    USING (true)
    WITH CHECK (true);

DROP POLICY IF EXISTS "allow_all_comandos" ON public.buscador_comandos;
CREATE POLICY "allow_all_comandos"
    ON public.buscador_comandos
    FOR ALL
    USING (true)
    WITH CHECK (true);
