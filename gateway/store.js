'use strict';

const fs = require('fs');
const path = require('path');

class PersistentStore {
    constructor(filePath) {
        this.filePath = filePath;
        this.data = {
            qris: {},      // qrisId -> entry
            orders: {},    // orderId -> qrisId
            claimed: {}    // txId -> { qrisId, claimedAt }
        };
        this._load();
    }

    _load() {
        try {
            if (fs.existsSync(this.filePath)) {
                const raw = fs.readFileSync(this.filePath, 'utf8');
                const parsed = JSON.parse(raw);
                this.data.qris = parsed.qris || {};
                this.data.orders = parsed.orders || {};
                this.data.claimed = parsed.claimed || {};
                
                // Hydrate Date objects
                for (const qid in this.data.qris) {
                    const item = this.data.qris[qid];
                    if (item.expiresAt) item.expiresAt = new Date(item.expiresAt);
                    if (item.createdAt) item.createdAt = new Date(item.createdAt);
                }
                console.log(`[PersistentStore] Loaded ${Object.keys(this.data.qris).length} QRIS entries, ${Object.keys(this.data.claimed).length} claimed txs`);
            }
        } catch (err) {
            console.error('[PersistentStore] Error loading state:', err.message);
        }
    }

    _save() {
        try {
            const dir = path.dirname(this.filePath);
            if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
            
            // Atomic write: write to tmp then rename
            const tmpFile = `${this.filePath}.tmp.${Date.now()}`;
            fs.writeFileSync(tmpFile, JSON.stringify(this.data, null, 2), 'utf8');
            fs.renameSync(tmpFile, this.filePath);
        } catch (err) {
            console.error('[PersistentStore] Error saving state:', err.message);
        }
    }

    // QRIS operations
    setQris(qrisId, entry) {
        this.data.qris[qrisId] = entry;
        if (entry.orderId) {
            this.data.orders[entry.orderId] = qrisId;
        }
        this._save();
    }

    getQris(qrisId) {
        return this.data.qris[qrisId] || null;
    }

    getQrisByOrderId(orderId) {
        const qrisId = this.data.orders[orderId];
        if (qrisId && this.data.qris[qrisId]) {
            return { qrisId, entry: this.data.qris[qrisId] };
        }
        // Fallback scan
        for (const [qid, entry] of Object.entries(this.data.qris)) {
            if (entry.orderId === orderId) {
                this.data.orders[orderId] = qid;
                return { qrisId: qid, entry };
            }
        }
        return null;
    }

    deleteQris(qrisId) {
        const entry = this.data.qris[qrisId];
        if (entry && entry.orderId) {
            delete this.data.orders[entry.orderId];
        }
        delete this.data.qris[qrisId];
        this._save();
    }

    getAllQrisEntries() {
        return Object.entries(this.data.qris);
    }

    // Claimed transactions operations
    getClaim(txId) {
        return this.data.claimed[txId] || null;
    }

    setClaim(txId, claimData) {
        this.data.claimed[txId] = claimData;
        this._save();
    }
}

module.exports = { PersistentStore };
