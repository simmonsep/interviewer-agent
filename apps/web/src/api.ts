export class ApiError extends Error {code:string;status:number;serverMessage?:string;constructor(code:string,status:number,serverMessage?:string){super(code);this.code=code;this.status=status;this.serverMessage=serverMessage;}}
export async function api<T>(url:string,options:RequestInit={}):Promise<T>{
 const response=await fetch(url,{...options,credentials:'same-origin',cache:'no-store',headers:{...(options.body?{'Content-Type':'application/json'}:{}),...options.headers}});
 const value=await response.json().catch(()=>({code:'invalid_response'}));
 if(!response.ok)throw new ApiError(typeof value.code==='string'?value.code:'request_failed',response.status,
  typeof value.message==='string'?value.message:undefined);
 return value as T;
}
export const post=<T,>(url:string,body:unknown,key?:string)=>api<T>(url,{method:'POST',headers:key?{'Idempotency-Key':key}:{},body:JSON.stringify(body)});
export function describeError(error:unknown){
 if(error instanceof ApiError){
  if(error.status===401)return '登录已过期，请重新登录。';
  if(error.status===404)return '这场练习已不可用，或不属于当前账号。';
  if(error.status===409)return '内容或状态已变化，正在读取已保存的记录，请勿重复创建回答。';
  if(error.status===400)return '请检查输入：回答不超过2000字、40行，且不能仅包含空白。';
  if(error.status===403)return '当前请求未获允许，请从本地训练页面重新进入。';
 }
 return '请求未完成。你的输入仍保留，可以稍后重试。';
}
