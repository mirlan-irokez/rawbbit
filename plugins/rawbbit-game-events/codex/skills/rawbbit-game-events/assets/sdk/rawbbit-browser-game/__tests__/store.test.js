import test from 'node:test';
import assert from 'node:assert/strict';
import { IDBFactory, forceCloseDatabase } from 'fake-indexeddb';
import { BrowserGameStore } from '../store.js';

function initialMeta() {
  return {
    schemaVersion: 1,
    storeInstanceId: 'd3d5dd20-3213-4dfe-bc40-570781cf92b2',
    installationId: 'installation', endpoint: 'https://collector.example',
    permissionEpoch: 0, endpointRevision: 0, queueRevision: 0,
    sessionRevision: 0, sequence: 0,
    session: { id: null, lastActivity: 0, counter: 0 },
    counters: { expired: 0, evicted: 0, quarantinedExpired: 0, quarantinedEvicted: 0, discarded: 0 },
    retry: { attempt: 0, notBefore: 0 }, fault: null, claim: null,
    queueBytes: 0, adaptiveMaxEvents: 500,
  };
}

function row(sequence = 1) {
  return { handle: `handle-${sequence}`, payloadJson: '{not valid JSON', bytes: 15,
    sequence, enqueuedAt: 100, quarantined: false };
}

function rawOpen(indexedDB, name, version, upgrade) {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(name, version);
    request.onupgradeneeded = () => upgrade?.(request.result, request.transaction);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function committed(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = resolve;
    tx.onabort = () => reject(tx.error);
  });
}

function setup(overrides = {}) {
  const options = { indexedDB: new IDBFactory(), name: 'game', initialMeta: initialMeta(), ...overrides };
  return { options, open: () => BrowserGameStore.open(options) };
}

test('committed records, metadata and receipts reopen unchanged and sequence-sorted', async () => {
  const { open, options } = setup();
  const store = await open();
  assert.equal(store.name, 'game');
  assert.equal(store.faulted, false);
  assert.equal(await store.update(state => {
    state.records.push(row(2), row(1));
    state.meta.sequence = 2;
    state.meta.session.counter = 2;
    state.receipts.push({ operationId: 'operation', reason: 'discarded' });
    return 'committed';
  }), 'committed');
  const before = await store.read();
  assert.deepEqual(before.records.map(record => record.sequence), [1, 2]);
  store.close();
  options.initialMeta.installationId = 'must-not-replace-existing';
  const reopened = await open();
  assert.deepEqual(await reopened.read(), before);
  reopened.close();
});

test('new initialization is atomic and generates a secure UUID when absent', async () => {
  const { open, options } = setup();
  delete options.initialMeta.storeInstanceId;
  const store = await open();
  assert.match((await store.read()).meta.storeInstanceId,
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  store.close();
});

test('throwing synchronous mutator rolls back session, records and receipts', async () => {
  const { open } = setup();
  const store = await open();
  const before = await store.read();
  const cause = new Error('adapter failed');
  await assert.rejects(store.update(state => {
    state.meta.session.counter++;
    state.records.push(row());
    state.receipts.push({ operationId: 'not-committed' });
    throw cause;
  }), error => error === cause);
  assert.deepEqual(await store.read(), before);
  assert.equal(store.faulted, false);
  store.close();
});

test('async mutators are rejected and cannot commit before or after await', async () => {
  const { open } = setup();
  const store = await open();
  const before = await store.read();
  await assert.rejects(store.update(async state => {
    state.records.push(row());
    state.meta.session.counter++;
    await Promise.resolve();
    state.meta.sequence++;
    throw new Error('late rejection');
  }), { code: 'storageAsyncMutator' });
  assert.deepEqual(await store.read(), before);
  store.close();
});

test('successful write requests followed by transaction abort never resolve acceptance', async () => {
  const { open } = setup();
  const store = await open();
  const original = store._db.transaction.bind(store._db);
  store._db.transaction = (...args) => {
    const tx = original(...args);
    const records = tx.objectStore('records');
    const put = records.put.bind(records);
    records.put = (...values) => {
      const request = put(...values);
      request.onsuccess = () => tx.abort();
      return request;
    };
    return tx;
  };
  await assert.rejects(store.update(state => {
    state.records.push(row());
    state.meta.session.counter++;
    return 'not-committed';
  }), { code: 'storageFailure' });
  assert.equal(store.faulted, true);
  const reopened = await open();
  const snapshot = await reopened.read();
  assert.deepEqual(snapshot.records, []);
  assert.equal(snapshot.meta.session.counter, 0);
  reopened.close();
});

test('immutable identity/schema and invalid record structure roll back', async () => {
  const { open } = setup();
  const store = await open();
  const before = await store.read();
  await assert.rejects(store.update(state => { state.meta.storeInstanceId = 'replacement'; }),
    { code: 'storageImmutableMetadata' });
  await assert.rejects(store.update(state => { state.meta.schemaVersion = 2; }));
  await assert.rejects(store.update(state => { state.records.push({ ...row(), bytes: 0 }); }),
    { code: 'storageInvalidState' });
  await assert.rejects(store.update(state => { state.meta.queueRevision = Number.MAX_SAFE_INTEGER + 1; }));
  assert.deepEqual(await store.read(), before);
  store.close();
});

test('updates write only changed rows and delete exact removed keys', async () => {
  const { open } = setup();
  const store = await open();
  await store.update(state => {
    state.records.push(row(1), row(2), row(3));
    state.receipts.push({ operationId: 'old' }, { operationId: 'kept' });
  });
  const writes = [];
  const realTransaction = store._db.transaction.bind(store._db);
  store._db.transaction = (...args) => {
    const tx = realTransaction(...args);
    const realStore = tx.objectStore.bind(tx);
    const instrumented = new Set();
    tx.objectStore = name => {
      const objectStore = realStore(name);
      if (instrumented.has(name)) return objectStore;
      instrumented.add(name);
      for (const method of ['put', 'delete', 'clear']) {
        const original = objectStore[method].bind(objectStore);
        objectStore[method] = (...values) => {
          writes.push([name, method, values[0]]);
          return original(...values);
        };
      }
      return objectStore;
    };
    return tx;
  };
  await store.update(state => {
    state.records[1].quarantined = true;
    state.records.pop();
    state.receipts = state.receipts.filter(receipt => receipt.operationId !== 'old');
  });
  assert.deepEqual(writes.map(([name, method, value]) => [name, method, value?.handle ?? value]),
    [['records', 'put', 'handle-2'], ['records', 'delete', 'handle-3'], ['receipts', 'delete', 'old']]);
  store.close();
});

test('receipt order survives reopen with nonlexical UUIDs and clock rollback', async () => {
  const { open } = setup();
  const store = await open();
  const receipts = [
    { operationId: 'f1111111-1111-4111-8111-111111111111', order: 1, createdAt: 3000 },
    { operationId: '11111111-1111-4111-8111-111111111111', order: 2, createdAt: 2000 },
    { operationId: '81111111-1111-4111-8111-111111111111', order: 3, createdAt: 1000 },
  ];
  await store.update(state => {
    state.receipts.push(receipts[2], receipts[0], receipts[1]);
    state.meta.receiptSequence = 3;
  });
  assert.deepEqual((await store.read()).receipts, receipts);
  store.close();
  const reopened = await open();
  assert.deepEqual((await reopened.read()).receipts, receipts);
  await reopened.update(state => {
    assert.deepEqual(state.receipts, receipts);
    state.receipts.push({ operationId: '21111111-1111-4111-8111-111111111111', order: 4, createdAt: 0 });
    state.meta.receiptSequence = 4;
  });
  assert.deepEqual((await reopened.read()).receipts.map(receipt => receipt.order), [1, 2, 3, 4]);
  reopened.close();
});

test('legacy receipts sort by createdAt then operationId with zero fallback', async () => {
  const { open } = setup();
  const store = await open();
  await store.update(state => {
    state.receipts.push({ operationId: 'later', createdAt: 10 },
      { operationId: 'zero-z' }, { operationId: 'zero-a' },
      { operationId: 'earlier-z', createdAt: 5 }, { operationId: 'earlier-a', createdAt: 5 });
  });
  assert.deepEqual((await store.read()).receipts.map(receipt => receipt.operationId),
    ['zero-a', 'zero-z', 'earlier-a', 'earlier-z', 'later']);
  store.close();
});

test('optional receipt order must be a unique positive safe integer', async () => {
  const { open } = setup();
  const store = await open();
  for (const order of [undefined, null, 0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1]) {
    await assert.rejects(store.update(state => {
      state.receipts.push({ operationId: 'invalid-order', order });
    }), { code: 'storageInvalidState' });
  }
  await assert.rejects(store.update(state => {
    state.receipts.push({ operationId: 'first', order: 1 }, { operationId: 'second', order: 1 });
  }), { code: 'storageInvalidState' });
  assert.deepEqual((await store.read()).receipts, []);
  store.close();
});

test('unsupported or missing existing metadata fails closed without overwriting', async () => {
  for (const metadata of [undefined, { ...initialMeta(), schemaVersion: 99 }]) {
    const { open, options } = setup();
    const first = await open();
    first.close();
    const raw = await rawOpen(options.indexedDB, options.name, 1);
    const tx = raw.transaction(['meta', 'records'], 'readwrite');
    const done = committed(tx);
    if (metadata) tx.objectStore('meta').put(metadata, 'state');
    else tx.objectStore('meta').delete('state');
    tx.objectStore('records').put(row());
    await done;
    raw.close();
    await assert.rejects(open(), { code: 'storageUnsupportedSchema' });
    const inspect = await rawOpen(options.indexedDB, options.name, 1);
    const read = inspect.transaction(['meta', 'records']);
    const complete = committed(read);
    const meta = read.objectStore('meta').get('state');
    const records = read.objectStore('records').getAll();
    await complete;
    assert.deepEqual(meta.result, metadata);
    assert.deepEqual(records.result, [row()]);
    inspect.close();
  }
});

test('higher database versions remain untouched and diagnostics contain only static codes', async () => {
  const faults = [];
  const { open, options } = setup({ onFault: code => faults.push(code) });
  const raw = await rawOpen(options.indexedDB, options.name, 2, db => {
    db.createObjectStore('private-data').put('secret', 'key');
  });
  raw.close();
  await assert.rejects(open(), { code: 'storageUnsupportedVersion' });
  assert.deepEqual(faults, ['storageUnsupportedVersion']);
  const inspect = await rawOpen(options.indexedDB, options.name, 2);
  assert.equal(inspect.version, 2);
  assert.deepEqual([...inspect.objectStoreNames], ['private-data']);
  inspect.close();
});

test('failed initial metadata aborts the entire upgrade and leaks no raw failure codes', async () => {
  const faults = [];
  const { open, options } = setup({ onFault: code => faults.push(code) });
  options.initialMeta.retry = null;
  await assert.rejects(open(), { code: 'storageUnsupportedSchema' });
  assert.deepEqual(await options.indexedDB.databases(), []);
  assert.deepEqual(faults, ['storageUnsupportedSchema']);
});

test('versionchange closes the old capability without reopening after deletion', async () => {
  const faults = [];
  const { open, options } = setup({ onFault: code => faults.push(code) });
  const store = await open();
  await new Promise((resolve, reject) => {
    const request = options.indexedDB.deleteDatabase(options.name);
    request.onsuccess = resolve;
    request.onerror = () => reject(request.error);
  });
  assert.equal(store.faulted, true);
  assert.equal(store.failureCode, 'storageVersionChanged');
  assert.deepEqual(faults, ['storageVersionChanged']);
  await assert.rejects(store.read(), { code: 'storageVersionChanged' });
  await assert.rejects(store.update(() => {}), { code: 'storageVersionChanged' });
  assert.deepEqual(await options.indexedDB.databases(), []);
});

test('unexpected connection close faults once; explicit close also prevents reuse', async () => {
  const faults = [];
  const { open } = setup({ onFault: code => { faults.push(code); throw new Error('host'); } });
  const store = await open();
  forceCloseDatabase(store._db);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(store.failureCode, 'storageClosed');
  store.close();
  assert.deepEqual(faults, ['storageClosed']);
  await assert.rejects(store.read(), { code: 'storageClosed' });
});

test('blocked open times out; late success is closed rather than leaked', async () => {
  const faults = [];
  const { open, options } = setup({ timeoutMs: 20, onFault: code => faults.push(code) });
  const blocker = await rawOpen(options.indexedDB, options.name, 1);
  // An in-progress upgrade blocks the SDK's v1 open until it is aborted.
  let upgrade;
  let blocked;
  const reachedBlocked = new Promise(resolve => { blocked = resolve; });
  upgrade = options.indexedDB.open(options.name, 2);
  upgrade.onblocked = blocked;
  upgrade.onupgradeneeded = () => upgrade.transaction.abort();
  upgrade.onerror = event => event.preventDefault();
  await reachedBlocked;
  await assert.rejects(open(), { code: 'storageTimeout' });
  assert.deepEqual(faults, ['storageTimeout']);
  blocker.close();
  await new Promise(resolve => setTimeout(resolve, 30));
  // Deletion would be blocked indefinitely if the late successful open leaked.
  await new Promise((resolve, reject) => {
    const request = options.indexedDB.deleteDatabase(options.name);
    const timer = setTimeout(() => reject(new Error('late connection leaked')), 200);
    request.onsuccess = () => { clearTimeout(timer); resolve(); };
    request.onerror = () => { clearTimeout(timer); reject(request.error); };
  });
});

test('stalled transaction times out, aborts writes and never falsely accepts', async () => {
  const faults = [];
  const { open, options } = setup({ onFault: code => faults.push(code) });
  const store = await open();
  store._timeoutMs = 20;
  const original = store._db.transaction.bind(store._db);
  store._db.transaction = (...args) => {
    const tx = original(...args);
    // Keep the actual transaction active with requests while withholding the
    // store's completion notification, simulating a browser storage stall.
    const objectStore = tx.objectStore('meta');
    const keepAlive = () => {
      try { objectStore.get('state').onsuccess = keepAlive; } catch { /* Aborted. */ }
    };
    keepAlive();
    return tx;
  };
  await assert.rejects(store.update(state => {
    state.records.push(row());
    state.meta.session.counter++;
    return 'must-not-be-accepted';
  }), { code: 'storageTimeout' });
  assert.equal(store.faulted, true);
  assert.deepEqual(faults, ['storageTimeout']);
  const reopened = await BrowserGameStore.open({ ...options, onFault: undefined });
  assert.deepEqual((await reopened.read()).records, []);
  assert.equal((await reopened.read()).meta.session.counter, 0);
  reopened.close();
});
