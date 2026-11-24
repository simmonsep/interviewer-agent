// F07Z-146: the human sentence is what the user reads first; the status code and server message
// live behind one collapsed disclosure. Nothing here touches business state, and nothing renders
// a stack trace, SQL, file path, token or secret - only fields the API boundary already exposed.
import {ApiError} from './api.ts';

export interface OperationErrorDetail {
  status: number;
  code: string;
  message?: string;
}

/** Null for anything that is not an ApiError (network aborts have no server code to show). */
export function errorDetailOf(error: unknown): OperationErrorDetail | null {
  if (!(error instanceof ApiError)) return null;
  return {status: error.status, code: error.code, message: error.serverMessage};
}

export function OperationErrorDetails({detail, label = '查看技术详情'}: {
  detail: OperationErrorDetail | null;
  label?: string;
}) {
  if (!detail) return null;
  return <details className="d-error-details" data-testid="error-details">
    <summary>{label}</summary>
    <pre>{`HTTP ${detail.status} · ${detail.code}${detail.message ? `\n${detail.message}` : ''}`}</pre>
  </details>;
}
