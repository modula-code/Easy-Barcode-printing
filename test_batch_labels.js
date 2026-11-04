const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');

function setup() {
    const html = fs.readFileSync(`${__dirname}/templates/index.html`, 'utf8');
    const source = html.slice(html.indexOf('            function newEventId()'), html.indexOf('            function renderResults(data)'));
    const requests = [], windows = [], messages = [], stored = new Map();
    const context = vm.createContext({
        document: { createElement: () => ({
            addEventListener(name, callback) { this.click = () => callback({ preventDefault() {} }); },
            setAttribute() {}, removeAttribute() {},
        }) },
        resolvedPoNumber: 'PO001', workDateInput: { value: '2026-11-04', reportValidity: () => true },
        revisionAlertFor: () => null,
        sessionStorage: { getItem: k => stored.get(k), setItem: (k, v) => stored.set(k, v), removeItem: k => stored.delete(k) },
        window: { open: () => { const w = { close() {} }; windows.push(w); return w; } },
        showMessage: (...args) => messages.push(args),
        fetch: async (url, options) => {
            requests.push(JSON.parse(options.body));
            return { ok: true, json: async () => ({ sync: {} }) };
        },
    });
    vm.runInContext(source, context);
    return { context, requests, windows, messages, stored };
}

const repeated = { part_code: 'PART1', document_match: { all_token: 'tok' } };

test('chosen quantity prints that many pages once and sends the label token', async () => {
    const s = setup();
    const input = { reportValidity: () => true, valueAsNumber: 3 };
    const button = s.context.queuedPrintLink(repeated, '/print/tok', input, 'Print and record quantity');
    await Promise.all([button.click(), button.click()]);
    assert.equal(s.requests.length, 1);
    assert.equal(s.requests[0].quantity, 3);
    assert.equal(s.requests[0].label_token, 'tok');
    assert.equal(s.windows.length, 1);
    assert.equal(s.windows[0].location, '/print/tok?count=3');
    assert.equal(s.stored.size, 0);
    assert.match(s.messages[0][0], /Synced PART1 x 3/);
});

test('invalid quantity is blocked before sending', async () => {
    const s = setup();
    await s.context.queuedPrintLink(repeated, '/print/tok', { reportValidity: () => false }, 'Print').click();
    assert.equal(s.requests.length, 0);
    assert.equal(s.windows.length, 0);
});

test('failed requests retain their event ID for retry', async () => {
    const s = setup();
    s.context.fetch = async (url, options) => {
        s.requests.push(JSON.parse(options.body));
        throw new Error('Connection lost');
    };
    const button = s.context.queuedPrintLink(repeated, '/print/tok', { reportValidity: () => true, valueAsNumber: 2 }, 'Print');
    await button.click();
    await button.click();
    assert.equal(s.requests.length, 2);
    assert.equal(s.requests[0].event_id, s.requests[1].event_id);
});

test('existing print action still records its quantity and opens the PDF', async () => {
    const s = setup();
    await s.context.queuedPrintLink({ part_code: 'PART1' }, '/label.pdf', 1, 'Print').click();
    assert.equal(s.requests[0].quantity, 1);
    assert.equal(s.windows[0].location, '/label.pdf');
});
