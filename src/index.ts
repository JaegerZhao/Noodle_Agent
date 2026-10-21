import type { Context } from '@deepseek-ai/cordis'
import Schema from '@deepseek-ai/schemastery'
import { NoodleHttpProvider, type HttpProviderConfig } from './http-provider.js'
import * as ReadTools from './read-tools.js'
import * as WriteTools from './write-tools.js'
import * as AgentGuidance from './agent-guidance.js'
import * as WorkspaceBootstrap from './workspace-bootstrap.js'

export const name = 'noodle-dsh-bridge'

export interface Config extends Omit<HttpProviderConfig, 'apiPrefix'> {
  apiPrefix: string
  workspacePath: string
  workspaceTitle: string
}

export const Config: Schema<Config> = Schema.object({
  baseUrl: Schema.string().default('http://127.0.0.1:8000'),
  apiPrefix: Schema.string().default('/internal/v1'),
  retrievalMode: Schema.union(['HYBRID', 'KEYWORD_ONLY']).default('HYBRID'),
  timeoutMs: Schema.number().default(120_000),
  maxBodyChars: Schema.number().default(12_000),
  workspacePath: Schema.string().default(process.cwd()),
  workspaceTitle: Schema.string().default('Noodle Agent'),
})

/** Mount the HTTP capability provider plus read and approval-gated write tools. */
export function apply(ctx: Context, config: Config): void {
  if (!/^https?:\/\//.test(config.baseUrl)) {
    throw new Error('noodle-dsh-bridge: baseUrl must start with http:// or https://')
  }
  if (!config.apiPrefix.startsWith('/')) {
    throw new Error('noodle-dsh-bridge: apiPrefix must start with /')
  }
  if (!Number.isInteger(config.timeoutMs) || config.timeoutMs < 1) {
    throw new Error('noodle-dsh-bridge: timeoutMs must be a positive integer')
  }
  if (!Number.isInteger(config.maxBodyChars) || config.maxBodyChars < 256) {
    throw new Error('noodle-dsh-bridge: maxBodyChars must be an integer >= 256')
  }
  ctx.plugin(NoodleHttpProvider, config)
  ctx.plugin(ReadTools, { nextOnly: config.apiPrefix === "/internal/next" })
  if (config.apiPrefix !== "/internal/next") ctx.plugin(WriteTools)
  ctx.plugin(AgentGuidance, { nextOnly: config.apiPrefix === "/internal/next" })
  ctx.plugin(WorkspaceBootstrap, {
    path: config.workspacePath,
    title: config.workspaceTitle,
  })
}

export * from './contracts.js'
export * from './http-provider.js'
export * from './service.js'
