/* Checkout page: create the payment, show the UPI QR, poll until paid,
   then reveal the success view with a link to the receipt.               */

const $ = (id) => document.getElementById(id);
let orderRef = null;
let poll = null;

async function startCheckout() {
  const res = await fetch('/api/checkout', { method: 'POST' });
  const data = await res.json();
  if (!data.ok) {
    $('orderRefLine').textContent = 'Could not start payment. Returning to basket…';
    setTimeout(() => (window.location = '/shop'), 1800);
    return;
  }
  orderRef = data.order_ref;
  $('amount').textContent = '₹' + Number(data.total).toFixed(2);
  $('orderRefLine').textContent = 'Order ' + data.order_ref + ' · pay with any UPI app';
  if (data.qr) {
    $('qrImg').src = data.qr;
    $('qrImg').style.display = 'inline-block';
  }
  poll = setInterval(checkStatus, 2000);
}

async function checkStatus() {
  if (!orderRef) return;
  const res = await fetch('/api/payment/status/' + orderRef);
  const data = await res.json();
  if (data.status === 'paid') {
    clearInterval(poll);
    showSuccess();
  } else if (data.status === 'failed') {
    clearInterval(poll);
    $('payStatus').innerHTML = '<span style="color:var(--warn)">Payment failed. Try again.</span>';
  }
}

function showSuccess() {
  $('payView').style.display = 'none';
  $('successView').style.display = 'block';
  $('successRef').textContent = 'Order ' + orderRef;
  $('viewReceiptBtn').href = '/receipt/' + orderRef;
}

/* Print bill on the connected thermal printer. */
const printBtn = $('printBillBtn');
if (printBtn) {
  printBtn.addEventListener('click', async () => {
    if (!orderRef) return;
    const status = $('printStatus');
    printBtn.disabled = true;
    printBtn.textContent = 'Printing…';
    status.textContent = '';
    try {
      const res = await fetch('/api/receipt/print/' + orderRef, { method: 'POST' });
      const data = await res.json();
      if (data.ok && data.printed) {
        printBtn.textContent = '✓ Printed';
        status.textContent = 'Collect your bill from the printer.';
        setTimeout(() => {
          printBtn.disabled = false;
          printBtn.textContent = '🖨 Print again';
        }, 2500);
      } else {
        throw new Error(data.error || 'print_failed');
      }
    } catch (err) {
      printBtn.disabled = false;
      printBtn.textContent = '🖨 Print bill';
      status.innerHTML = '<span style="color:var(--warn)">Printer unavailable — use “View receipt”.</span>';
      console.error('print failed', err);
    }
  });
}

const simBtn = $('simPayBtn');
if (simBtn) {
  simBtn.addEventListener('click', async () => {
    if (!orderRef) return;
    await fetch('/api/payment/confirm/' + orderRef, { method: 'POST' });
    simBtn.disabled = true;
    simBtn.textContent = 'Confirming…';
    checkStatus();
  });
}

startCheckout();
