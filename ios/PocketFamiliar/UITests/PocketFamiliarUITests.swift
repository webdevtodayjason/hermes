import XCTest

/// End-to-end against the fake gateway (tests/fake_gateway.py on :8999):
/// it pushes deck + permission frames on connect and echoes every received
/// command back as a visible notify — so what the app *sent* is asserted on
/// screen, and the exact wire JSON is asserted by the server's RX log.
final class PocketFamiliarUITests: XCTestCase {

    func testDeckTapHoldConfirmAndApprovalResolve() throws {
        let app = XCUIApplication()
        app.launchArguments = ["-host", "127.0.0.1", "-port", "8999",
                               "-token", "uitest-token", "-tab", "ops"]
        app.launch()
        let tabs = app.tabBars

        // deck frame rendered
        let go = app.staticTexts["GOBTN"]
        XCTAssertTrue(go.waitForExistence(timeout: 15), "deck button never rendered")
        go.tap()

        // plain tap fired -> server echo lands in feed
        tabs.buttons["Feed"].tap()
        XCTAssertTrue(app.staticTexts["deck-ok-0"].waitForExistence(timeout: 10),
                      "deck cmd for button 0 never reached the gateway")

        // confirm-flagged button: tap must NOT fire, hold must
        tabs.buttons["Ops"].tap()
        let risk = app.staticTexts["RISKBTN"]
        XCTAssertTrue(risk.waitForExistence(timeout: 5))
        risk.tap()
        tabs.buttons["Feed"].tap()
        XCTAssertFalse(app.staticTexts["deck-ok-1"].waitForExistence(timeout: 3),
                       "confirm button fired on plain tap")
        tabs.buttons["Ops"].tap()
        risk.press(forDuration: 1.2)
        tabs.buttons["Feed"].tap()
        XCTAssertTrue(app.staticTexts["deck-ok-1"].waitForExistence(timeout: 10),
                      "confirm button did not fire on hold")

        // approval banner is live: ALLOW ONCE resolves via permission frame
        tabs.buttons["Face"].tap()
        let allow = app.buttons["ALLOW ONCE"]
        XCTAssertTrue(allow.waitForExistence(timeout: 5), "approval banner missing")
        allow.tap()
        XCTAssertFalse(allow.waitForExistence(timeout: 2), "banner not cleared after resolve")
        tabs.buttons["Feed"].tap()
        XCTAssertTrue(app.staticTexts["resolved-once"].waitForExistence(timeout: 10),
                      "permission decision never reached the gateway")
    }
}
