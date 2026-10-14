const STORES = ['meta', 'records', 'receipts'];
const REVISIONS = ['permissionEpoch', 'endpointRevision', 'queueRevision', 'sessionRevision', 'sequence'];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const token = value => typeof value === 'string' && value.length > 0;
const integer = value => Number.isSafeInteger(value) && value >= 0;

function failure(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function validate(snapshot) {
  const { meta, records, receipts } = snapshot;
  if (!object(meta) || meta.schemaVersion !== 1 || !token(meta.storeInstanceId) ||
      !token(meta.installationId) || typeof meta.endpoint !== 'string' ||
      !REVISIONS.every(key => integer(meta[key])) || !object(meta.session) ||
      !object(meta.counters) || !object(meta.retry) ||
      !(meta.fault === null || object(meta.fault)) || !(meta.claim === null || object(meta.claim))) {
    throw failure('storageUnsupportedSchema');
  }
  const handles = new Set();
  for (const record of records) {
    if (!object(record) || !token(record.handle) || handles.has(record.handle) ||
        typeof record.payloadJson !== 'string' || !integer(record.bytes) || record.bytes === 0 ||
        !integer(record.sequence) || record.sequence === 0 ||
        !Number.isFinite(record.enqueuedAt) || record.enqueuedAt < 0 ||
        typeof record.quarantined !== 'boolean') {
      throw failure('storageInvalidState');
    }
    handles.add(record.handle);
  }
  const operations = new Set();
  const receiptOrders = new Set();
  for (const receipt of receipts) {
    if (!object(receipt) || !token(receipt.operationId) || operations.has(receipt.operationId)) {
      throw failure('storageInvalidState');
    }
    if (Object.hasOwn(receipt, 'order')) {
      if (!integer(receipt.order) || receipt.order === 0 || receiptOrders.has(receipt.order)) {
        throw failure('storageInvalidState');
      }
      receiptOrders.add(receipt.order);
    }
    operations.add(receipt.operationId);
  }
}

// Compare large payload strings directly; serialize only small nested metadata.
function equalRow(left, right) {
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length && keys.every(key =>
    Object.hasOwn(right, key) && (left[key] === right[key] ||
      (object(left[key]) || Array.isArray(left[key])) &&
      JSON.stringify(left[key]) === JSON.stringify(right[key])));
}

function writeDiff(store, before, after, key) {
  const previous = new Map(before.map(row => [row[key], row]));
  for (const row of after) {
    const old = previous.get(row[key]);
    if (!old || !equalRow(old, row)) store.put(row);
    previous.delete(row[key]);
  }
  for (const removed of previous.keys()) store.delete(removed);
}

function layoutValid(db, tx) {
  if (!STORES.every(name => db.objectStoreNames.contains(name))) return false;
  const meta = tx.objectStore('meta');
  const records = tx.objectStore('records');
  const receipts = tx.objectStore('receipts');
  return meta.keyPath === null && !meta.autoIncrement &&
    records.keyPath === 'handle' && !records.autoIncrement &&
    receipts.keyPath === 'operationId' && !receipts.autoIncrement &&
    ['sequence', 'enqueuedAt'].every(name => records.indexNames.contains(name) &&
      records.index(name).keyPath === name && !records.index(name).unique &&
      !records.index(name).multiEntry);
}

export class BrowserGameStore {
  static async open({ indexedDB, name, initialMeta, onFault, timeoutMs = 3000 }) {
    if (!indexedDB || !token(name) || !Number.isFinite(timeoutMs) || timeoutMs <= 0) {
      throw failure('storageUnavailable');
    }
    const db = await new Promise((resolve, reject) => {
      let request;
      let settled = false;
      let upgradeError;
      const notify = code => {
        try { onFault?.(code); } catch { /* Host diagnostics cannot affect storage. */ }
      };
      const fail = code => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        try { request?.transaction?.abort(); } catch { /* Already inactive. */ }
        notify(code);
        reject(failure(code));
      };
      const timer = setTimeout(() => fail('storageTimeout'), timeoutMs);
      try {
        request = indexedDB.open(name, 1);
        request.onupgradeneeded = event => {
          if (settled) {
            request.transaction.abort();
            request.result.close();
            return;
          }
          try {
            if (event.oldVersion !== 0) throw failure('storageUnsupportedVersion');
            const meta = structuredClone(initialMeta);
            if (!object(meta)) throw failure('storageUnsupportedSchema');
            meta.schemaVersion = 1;
            meta.storeInstanceId ??= globalThis.crypto?.randomUUID?.();
            if (!UUID.test(meta.storeInstanceId)) throw failure('storageUnsupportedSchema');
            validate({ meta, records: [], receipts: [] });
            const database = request.result;
            database.createObjectStore('meta').put(meta, 'state');
            const records = database.createObjectStore('records', { keyPath: 'handle' });
            records.createIndex('sequence', 'sequence');
            records.createIndex('enqueuedAt', 'enqueuedAt');
            database.createObjectStore('receipts', { keyPath: 'operationId' });
          } catch (error) {
            upgradeError = ['storageUnsupportedSchema', 'storageUnsupportedVersion'].includes(error.code)
              ? error.code : 'storageFailure';
            request.transaction.abort();
          }
        };
        request.onerror = () => fail(upgradeError ||
          (request.error?.name === 'VersionError' ? 'storageUnsupportedVersion' : 'storageFailure'));
        // A blocked open remains bounded by the same deadline. A late connection
        // must be closed even if the caller has already received a timeout.
        request.onsuccess = () => {
          if (settled) {
            request.result.close();
            return;
          }
          settled = true;
          clearTimeout(timer);
          resolve(request.result);
        };
      } catch {
        fail('storageFailure');
      }
    });
    const store = new BrowserGameStore(db, onFault, timeoutMs);
    try {
      await store.read();
      return store;
    } catch (error) {
      store.close();
      throw error;
    }
  }

  constructor(db, onFault, timeoutMs) {
    this.name = db.name;
    this.faulted = false;
    this.failureCode = null;
    this._db = db;
    this._onFault = onFault;
    this._timeoutMs = timeoutMs;
    this._pending = new Set();
    db.onversionchange = () => this._fault('storageVersionChanged');
    db.onclose = () => this._fault('storageClosed');
  }

  _fault(code) {
    if (this.faulted) return;
    this.faulted = true;
    this.failureCode = code;
    for (const cancel of [...this._pending]) cancel(code);
    this._db.close();
    try { this._onFault?.(code); } catch { /* Only safe static codes leave the store. */ }
  }

  read() {
    return this._transaction();
  }

  update(mutator) {
    if (typeof mutator !== 'function') return Promise.reject(failure('storageInvalidMutator'));
    return this._transaction(mutator);
  }

  _transaction(mutator) {
    if (this.faulted) return Promise.reject(failure(this.failureCode));
    return new Promise((resolve, reject) => {
      let tx;
      let settled = false;
      let result;
      let error;
      const finish = (cause, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        this._pending.delete(cancel);
        if (cause) reject(cause);
        else resolve(value);
      };
      const cancel = code => {
        error = failure(code);
        try { tx?.abort(); } catch { /* The connection may already be closed. */ }
        finish(error);
      };
      const timer = setTimeout(() => this._fault('storageTimeout'), this._timeoutMs);
      this._pending.add(cancel);
      try {
        tx = this._db.transaction(STORES, mutator ? 'readwrite' : 'readonly');
        tx.oncomplete = () => finish(null, result);
        tx.onabort = () => {
          if (error) finish(error);
          else this._fault('storageFailure');
        };
        // Request errors abort by default; only abort/completion decides outcome.
        if (!layoutValid(this._db, tx)) {
          this._fault('storageUnsupportedSchema');
          return;
        }
        const metaRequest = tx.objectStore('meta').get('state');
        const recordsRequest = tx.objectStore('records').getAll();
        const receiptsRequest = tx.objectStore('receipts').getAll();
        let remaining = 3;
        const loaded = () => {
          if (--remaining || settled) return;
          const snapshot = {
            meta: metaRequest.result,
            records: recordsRequest.result.sort((a, b) => a.sequence - b.sequence),
            receipts: receiptsRequest.result,
          };
          try {
            validate(snapshot);
          } catch (cause) {
            this._fault(cause.code || 'storageInvalidState');
            return;
          }
          snapshot.receipts.sort((left, right) =>
            (left.order ?? left.createdAt ?? 0) - (right.order ?? right.createdAt ?? 0) ||
            (left.operationId < right.operationId ? -1 : left.operationId > right.operationId ? 1 : 0));
          if (!mutator) {
            result = snapshot;
            return;
          }
          try {
            const before = structuredClone(snapshot);
            result = mutator(snapshot);
            if (settled) return;
            if (result !== null && (typeof result === 'object' || typeof result === 'function') &&
                typeof result.then === 'function') {
              // Observe a rejected async callback without awaiting it in the transaction.
              Promise.resolve(result).catch(() => {});
              throw failure('storageAsyncMutator');
            }
            validate(snapshot);
            if (snapshot.meta.schemaVersion !== before.meta.schemaVersion ||
                snapshot.meta.storeInstanceId !== before.meta.storeInstanceId) {
              throw failure('storageImmutableMetadata');
            }
            if (!equalRow(before.meta, snapshot.meta)) tx.objectStore('meta').put(snapshot.meta, 'state');
            writeDiff(tx.objectStore('records'), before.records, snapshot.records, 'handle');
            writeDiff(tx.objectStore('receipts'), before.receipts, snapshot.receipts, 'operationId');
          } catch (cause) {
            // Callback failures roll back but do not poison an otherwise healthy DB.
            error = cause;
            try { tx.abort(); } catch { finish(error); }
          }
        };
        metaRequest.onsuccess = loaded;
        recordsRequest.onsuccess = loaded;
        receiptsRequest.onsuccess = loaded;
      } catch {
        this._fault('storageFailure');
      }
    });
  }

  close() {
    this._fault('storageClosed');
  }
}
