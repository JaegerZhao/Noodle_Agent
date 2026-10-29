import type { ToolExecutionInput } from '@deepseek-ai/dsh-tools'
import type { JsonObject, RequestContext } from './contracts.js'

const provenance = {type:'object' as const,additionalProperties:true,properties:{source_revision_id:{type:'string' as const},page:{type:'json' as const}}}
export const domainOutput = {
  schema: {type:'object' as const,additionalProperties:true,properties:{
    status:{type:'string' as const}, request_id:{type:'string' as const}, message:{type:'string' as const},
    answer_markdown:{type:'string' as const}, citations:{type:'array' as const,items:{type:'object' as const,additionalProperties:true,properties:{
      citation_id:{type:'string' as const},note_id:{type:'string' as const},revision_id:{type:'string' as const},section_id:{type:'string' as const},title:{type:'string' as const},provenance:{type:'array' as const,items:provenance}
    }}}
  }},
  render: (_args:unknown,value:unknown) => [{type:'text' as const,text:JSON.stringify(value)}],
}
export function requestContext(exec:ToolExecutionInput):RequestContext {
  const context:RequestContext={signal:exec.signal,callId:String(exec.callId),...(exec.agent?{sessionId:String(exec.agent.session.id)}:{})}
  for(const event of [...(exec.agent?.session.events||[])].reverse()){
    if(event.type!=='user/message'||event.data.source.kind!=='user')continue
    const source=event.data.source
    if('rpcId' in source&&typeof source.rpcId==='string')context.userRpcId=source.rpcId
    break
  }
  return context
}
const failures = new Map<string,{turn:number;status:string;message:string}>()
/** A configuration failure is not a reason to enumerate the entire library. */
export async function executeDomain(exec:ToolExecutionInput,run:()=>Promise<JsonObject>):Promise<JsonObject> {
  const session=exec.agent?.session
  const key=session?String(session.id):''
  const turn=session?[...session.events].reverse().find(e=>e.type==='turn/start')?.seq ?? -1:-1
  const failure=key?failures.get(key):undefined
  if(failure?.turn===turn&&!['noodle_get_readiness','noodle_get_task','noodle_get_source_status'].includes(exec.name))
    return {status:failure.status,message:failure.message,next_actions:['Report the configuration/provider failure. Do not enumerate notes or retry unchanged requests in this turn.']}
  const value=await run()
  if(key&&['PROFILE_MISMATCH','PROVIDER_ERROR'].includes(String(value.status))){
    failures.set(key,{turn,status:String(value.status),message:String(value.message||value.status)})
    if(failures.size>1000)failures.delete(failures.keys().next().value!)
    value.next_actions=['Stop this knowledge answer and report the failure. Use readiness diagnostics; do not traverse the library as fallback.']
  }
  return value
}
