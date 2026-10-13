import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-workspace'

export const name = 'noodle-workspace-bootstrap'
export const inject = ['workspaceRegistry']

export interface Config {
  path: string
  title: string
}

/** Register the configured local directory without touching Harness session data. */
export async function apply(ctx: Context, config: Config): Promise<void> {
  await ctx.workspaceRegistry.create(config.path, config.title)
}
