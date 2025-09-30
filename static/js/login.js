function handleCredentialResponse(response) {
    fetch('/google-login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ credential: response.credential })
    })
    .then(res => res.json())
    .then(data => {
        if (data.success) {
            window.location.href = '/';
        } else {
            alert('Login failed');
        }
    })
    .catch(err => console.error(err));
}
