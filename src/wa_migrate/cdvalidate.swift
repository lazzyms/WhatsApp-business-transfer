// Usage: cdvalidate <model.bin (bplist NSManagedObjectModel)> <store.sqlite>
// Opens the store read-only with WhatsApp's own CoreData model (from Z_MODELCACHE)
// and runs validateForUpdate on every object of every entity.
import CoreData
import Foundation

let args = CommandLine.arguments
let modelData = try Data(contentsOf: URL(fileURLWithPath: args[1]))
let unarch = try NSKeyedUnarchiver(forReadingFrom: modelData)
unarch.requiresSecureCoding = false
guard let model = unarch.decodeObject(forKey: NSKeyedArchiveRootObjectKey) as? NSManagedObjectModel else {
    print("FATAL: could not decode model"); exit(2)
}
// Custom classes (WAMessage...) don't exist on the Mac: use plain NSManagedObject.
for e in model.entities { e.managedObjectClassName = NSStringFromClass(NSManagedObject.self) }

let storeURL = URL(fileURLWithPath: args[2])
let psc = NSPersistentStoreCoordinator(managedObjectModel: model)
do {
    let meta = try NSPersistentStoreCoordinator.metadataForPersistentStore(ofType: NSSQLiteStoreType, at: storeURL, options: nil)
    print("model compatible with store metadata:", model.isConfiguration(withName: nil, compatibleWithStoreMetadata: meta))
    try psc.addPersistentStore(ofType: NSSQLiteStoreType, configurationName: nil, at: storeURL,
                               options: [NSReadOnlyPersistentStoreOption: true,
                                         NSSQLitePragmasOption: ["journal_mode": "WAL"]])
} catch { print("FATAL: cannot open store:", error); exit(3) }

let ctx = NSManagedObjectContext(concurrencyType: .privateQueueConcurrencyType)
ctx.persistentStoreCoordinator = psc
var totalBad = 0
ctx.performAndWait {
    for e in model.entities.sorted(by: { $0.name! < $1.name! }) where !e.isAbstract {
        let req = NSFetchRequest<NSManagedObject>(entityName: e.name!)
        req.includesSubentities = false
        req.returnsObjectsAsFaults = false
        do {
            let objs = try ctx.fetch(req)
            // CoreData silently skips rows it cannot materialize (e.g. a NULL in a mandatory
            // attribute) and only logs them. COUNT runs in SQL, so a shortfall exposes them.
            let expected = try ctx.count(for: req)
            var bad = max(0, expected - objs.count)
            var samples: [String: Int] = [:]
            if bad > 0 { samples["rows CoreData could not read (missing mandatory values)"] = bad }
            for o in objs {
                // touch every relationship so dangling foreign keys surface
                for (name, _) in e.relationshipsByName { _ = o.value(forKey: name) }
                do { try o.validateForUpdate() } catch let err as NSError {
                    bad += 1
                    let details = (err.userInfo[NSDetailedErrorsKey] as? [NSError]) ?? [err]
                    for d in details {
                        let key = "\(d.userInfo[NSValidationKeyErrorKey] ?? "?"): \(d.localizedDescription)"
                        samples[key, default: 0] += 1
                    }
                }
            }
            totalBad += bad
            print(String(format: "%-26@ %6d objects, %6d invalid", e.name! as NSString, objs.count, bad))
            for (k, n) in samples.sorted(by: { $0.value > $1.value }).prefix(8) { print("     \(n)x  \(k)") }
        } catch { print("FETCH FAILED \(e.name!): \(error)"); totalBad += 1 }
    }
}
print(totalBad == 0 ? "RESULT: VALID" : "RESULT: INVALID (\(totalBad))")
exit(totalBad == 0 ? 0 : 1)
