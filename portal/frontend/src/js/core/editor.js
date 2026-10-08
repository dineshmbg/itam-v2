// Edit schema (which fields exist, which are choices, what the signed-in group may change).
import { get } from './api.js';

let schemaP;
export const getSchema = () => (schemaP ||= get('/api/edit/schema').catch((e) => { schemaP = null; throw e; }));
export const resetSchema = () => { schemaP = null; };

