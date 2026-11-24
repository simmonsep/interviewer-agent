import { betterAuth, type BetterAuthOptions } from "better-auth";
import type { Pool } from "pg";
import { randomUUID } from "node:crypto";
export interface DevConfig { mode: string; host: string; port: number; baseURL: string; secret: string }
export function assertDevelopmentConfig(config: DevConfig) {
  const url=new URL(config.baseURL);
  if(!["development","test"].includes(config.mode)||config.host!=="127.0.0.1"||url.hostname!=="127.0.0.1"||url.protocol!=="http:"||url.port!==String(config.port)||url.pathname!=="/")throw Error("development_loopback_only");
  if(config.secret.length<32)throw Error("auth_secret_required");
}
export function authOptions(pool: Pool, config: DevConfig) {
  assertDevelopmentConfig(config);
  return { database:pool, baseURL:config.baseURL,secret:config.secret, trustedOrigins:[config.baseURL],
    emailAndPassword:{enabled:true,requireEmailVerification:false,minPasswordLength:12},
    session:{cookieCache:{enabled:false}},
    advanced:{useSecureCookies:false,database:{generateId:()=>randomUUID()}},
    telemetry:{enabled:false},logger:{disabled:true},
  } satisfies BetterAuthOptions;
}
export function createDevAuth(pool: Pool, config: DevConfig) {return betterAuth(authOptions(pool,config));}
