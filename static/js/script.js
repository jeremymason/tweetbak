$(document).ready( function() {
    $('div.flash-msg').delay(5000).hide('fast', function () {
        $(this).remove();
    });
});

